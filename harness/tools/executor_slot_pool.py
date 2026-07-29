"""Runtime-owned admission for the homogeneous session-sandbox pool.

The model sees one logical execution environment.  The Harness alone chooses
one of the identically attested Unix sockets, keeps persistent processes
affine to that socket, and never admits more than one untrusted execution per
physical fixed-UID worker.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import PurePosixPath
import threading
import time
from typing import Awaitable, Callable, Literal


DEFAULT_EXECUTOR_SOCKET = "/run/chat-ds-executor/executor.sock"
MAX_EXECUTOR_POOL_SLOTS = 16
EXECUTOR_POOL_SOCKETS_ENV = "EXECUTOR_POOL_SOCKETS"
EXECUTOR_SLOT_REPROBE_TIMEOUT_SECONDS = 40.0
EXECUTOR_SLOT_REPROBE_INITIAL_BACKOFF_SECONDS = 5.0
EXECUTOR_SLOT_REPROBE_MAX_BACKOFF_SECONDS = 60.0
ReservationKind = Literal["transient", "persistent"]
ExecutorSlotReprobe = Callable[[str], Awaitable[str]]

_RECOVERABLE_QUARANTINE_REASONS = frozenset({
    "executor_connect_unavailable",
    "executor_quarantined",
    "executor_reported_worker_busy",
    "one_shot_cancelled_after_dispatch",
    "one_shot_dispatch_state_unknown",
    "one_shot_transport_state_unknown",
    "process_open_cancelled_unknown",
    "process_open_dispatch_unknown",
    "process_open_transport_unavailable",
    "worker_admission_lost",
    "worker_containment_failed",
})


class ExecutorSlotPoolError(RuntimeError):
    """Stable pool configuration or admission failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validated_socket_path(value: str) -> str:
    path = value.strip()
    candidate = PurePosixPath(path)
    if (
        not path
        or "\x00" in path
        or not candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts[1:])
        or len(path.encode("utf-8", errors="strict")) > 512
    ):
        raise ExecutorSlotPoolError(
            "executor_pool_configuration_invalid",
            "Executor pool sockets must be bounded absolute Unix paths.",
        )
    return path


def configured_executor_socket_paths(
    *,
    primary_socket: str | None = None,
) -> tuple[str, ...]:
    """Return the exact ordered physical pool hidden behind the logical lane."""

    raw_pool = os.environ.get(EXECUTOR_POOL_SOCKETS_ENV, "").strip()
    if raw_pool:
        raw_values = raw_pool.split(",")
        if any(not value.strip() for value in raw_values):
            raise ExecutorSlotPoolError(
                "executor_pool_configuration_invalid",
                "Executor pool socket entries cannot be empty.",
            )
        values = tuple(_validated_socket_path(value) for value in raw_values)
    else:
        selected = (
            primary_socket
            if primary_socket is not None
            else os.environ.get("EXECUTOR_SOCKET", DEFAULT_EXECUTOR_SOCKET)
        )
        values = (_validated_socket_path(selected),)
    if not values or len(values) > MAX_EXECUTOR_POOL_SLOTS:
        raise ExecutorSlotPoolError(
            "executor_pool_configuration_invalid",
            "Executor pool size is outside the bounded topology policy.",
        )
    if len(set(values)) != len(values):
        raise ExecutorSlotPoolError(
            "executor_pool_configuration_invalid",
            "Executor pool sockets must be unique.",
        )
    configured_primary = (
        primary_socket
        if primary_socket is not None
        else os.environ.get("EXECUTOR_SOCKET", values[0])
    )
    if raw_pool and _validated_socket_path(configured_primary) != values[0]:
        raise ExecutorSlotPoolError(
            "executor_pool_configuration_invalid",
            "EXECUTOR_SOCKET must identify the first executor pool slot.",
        )
    return values


def executor_pool_identity_sha256(
    socket_paths: tuple[str, ...],
    *,
    runtime_profile: str,
) -> str:
    """Bind runtime authority to every ordered physical pool member."""

    encoded = json.dumps(
        {
            "schema": "chatds-homogeneous-executor-pool-v1",
            "runtime_profile": runtime_profile,
            "socket_paths": list(socket_paths),
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def executor_attestation_sha256(response: dict[str, object]) -> str:
    """Hash a validated capability receipt without its per-request nonce."""

    stable = {
        key: value
        for key, value in response.items()
        if key != "request_id"
    }
    try:
        encoded = json.dumps(
            stable,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExecutorSlotPoolError(
            "executor_attestation_invalid",
            "Executor capability attestation is not canonical JSON.",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _valid_attestation_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(slots=True)
class _ExecutorSlot:
    socket_path: str
    state: str = "free"
    reservation_token: str | None = None
    reservation_kind: ReservationKind | None = None
    attestation_sha256: str | None = None
    quarantine_reason: str | None = None
    quarantine_recoverable: bool = False
    reprobe_failure_count: int = 0
    reprobe_not_before: float = 0.0
    reprobe_token: str | None = None


@dataclass(slots=True)
class _AdmissionWaiter:
    sequence: int
    kind: ReservationKind
    excluded_sockets: frozenset[str]
    future: asyncio.Future["ExecutorSlotReservation"]
    cancel_event: asyncio.Event | None = field(default=None, repr=False)


@dataclass(slots=True)
class ExecutorSlotReservation:
    """Opaque runtime capability for exactly one physical worker."""

    socket_path: str
    kind: ReservationKind
    _pool: "ExecutorSlotPool" = field(repr=False)
    _token: str = field(repr=False)
    _terminal: bool = field(default=False, repr=False)

    @property
    def terminal(self) -> bool:
        return self._terminal

    async def _finalize(self, *, quarantine_reason: str | None) -> None:
        """Publish one terminal slot transition despite caller cancellation.

        ``asyncio.shield`` alone is insufficient here: it keeps the inner
        transition alive, but raises cancellation to the caller before the
        reservation can record that the transition completed.  A process-close
        caller could then retain a stale leased capability even though the
        physical slot was already free.  Drain the runtime-owned transition to
        completion, remember its terminal state, and only then re-deliver
        cancellation.
        """

        if self._terminal:
            return
        finishing = asyncio.create_task(
            self._pool._finish_reservation(
                self,
                quarantine_reason=quarantine_reason,
            )
        )
        cancellation: asyncio.CancelledError | None = None
        while not finishing.done():
            try:
                await asyncio.shield(finishing)
            except asyncio.CancelledError as exc:
                cancellation = exc
                continue
        # Retrieve a possible internal failure before claiming the capability is
        # terminal.  The pool transition itself is deliberately side-effect
        # free apart from its lock-protected state update.
        finishing.result()
        self._terminal = True
        if cancellation is not None:
            raise cancellation

    async def release(self) -> None:
        await self._finalize(quarantine_reason=None)

    async def quarantine(self, reason: str) -> None:
        if self._terminal:
            return
        safe_reason = str(reason or "execution_state_unknown")[:160]
        await self._finalize(quarantine_reason=safe_reason)


class ExecutorSlotPool:
    """FIFO, cancellation-safe admission over homogeneous fixed-UID slots."""

    def __init__(self, socket_paths: tuple[str, ...]):
        if not socket_paths or len(set(socket_paths)) != len(socket_paths):
            raise ExecutorSlotPoolError(
                "executor_pool_configuration_invalid",
                "ExecutorSlotPool requires unique socket paths.",
            )
        self.socket_paths = tuple(socket_paths)
        self._slots = [
            _ExecutorSlot(socket_path=socket_path)
            for socket_path in socket_paths
        ]
        self._lock = asyncio.Lock()
        self._waiters: list[_AdmissionWaiter] = []
        self._next_waiter_sequence = 0
        self._next_reservation_token = 0
        self._next_reprobe_token = 0
        self._slot_cursor = 0
        self._homogeneous_attestation_sha256: str | None = None
        self._reprobe_handler: ExecutorSlotReprobe | None = None
        self._reprobe_tasks: dict[str, asyncio.Task[bool]] = {}
        # Synchronous dependency preflight cannot await the admission lock.
        # Publish only an immutable, atomically replaced routing projection;
        # every authoritative execution admission still uses ``_lock``.
        self._probe_socket_paths = tuple(socket_paths)

    def _healthy_slot_count_locked(self) -> int:
        return sum(
            slot.state not in {"quarantined", "unhealthy", "reaping"}
            for slot in self._slots
        )

    def _persistent_limit_locked(self) -> int:
        healthy = self._healthy_slot_count_locked()
        if len(self._slots) == 1:
            # Backward-compatible single-slot deployments cannot reserve a
            # transient lane, but remain safe because the worker is serialized.
            return 1 if healthy else 0
        return max(0, healthy - 1)

    def _persistent_count_locked(self) -> int:
        return sum(
            slot.state == "leased"
            and slot.reservation_kind == "persistent"
            for slot in self._slots
        )

    def _waiter_is_eligible_locked(
        self,
        waiter: _AdmissionWaiter,
    ) -> bool:
        if waiter.kind == "persistent":
            return (
                self._persistent_count_locked()
                < self._persistent_limit_locked()
            )
        return True

    def _ordered_free_slots_locked(
        self,
        excluded_sockets: frozenset[str],
    ) -> list[_ExecutorSlot]:
        ordered = [
            self._slots[(self._slot_cursor + offset) % len(self._slots)]
            for offset in range(len(self._slots))
        ]
        return [
            slot
            for slot in ordered
            if slot.state == "free"
            and slot.socket_path not in excluded_sockets
        ]

    def _publish_probe_socket_paths_locked(self) -> None:
        self._probe_socket_paths = tuple(
            slot.socket_path
            for slot in self._slots
            if slot.state not in {"quarantined", "unhealthy", "reaping"}
        )

    def probe_socket_paths(self) -> tuple[str, ...]:
        """Return the latest immutable healthy-candidate projection.

        Capability probes are read-only controller requests and may safely run
        while a worker reservation is active.  The projection intentionally
        excludes every slot whose execution state is quarantined or unhealthy.
        """

        return self._probe_socket_paths

    def configure_reprobe_handler(
        self,
        handler: ExecutorSlotReprobe,
    ) -> None:
        """Install the trusted controller recovery hook.

        The hook must first prove the worker empty and then return the digest
        of a freshly validated runtime-capability receipt.  A recovered slot
        is admitted only when that digest exactly matches the homogeneous
        cohort selected during startup attestation.
        """

        if not callable(handler):
            raise ExecutorSlotPoolError(
                "executor_pool_configuration_invalid",
                "Executor slot re-probe handler must be callable.",
            )
        if (
            self._homogeneous_attestation_sha256 is None
            or self._waiters
            or self._reprobe_tasks
            or any(
                slot.reservation_token is not None
                or slot.state == "reaping"
                for slot in self._slots
            )
        ):
            raise ExecutorSlotPoolError(
                "executor_pool_reprobe_configuration_race",
                "Executor slot re-probe must be configured after homogeneous "
                "startup attestation and before admission.",
            )
        # FastAPI lifespan may be entered again in one process during bounded
        # test/reload cycles. Each startup attestation independently proves
        # there are no active reservations before replacing this trusted hook;
        # an already-running re-probe has captured its original callable.
        self._reprobe_handler = handler

    async def _run_slot_reprobe(
        self,
        *,
        socket_path: str,
        reprobe_token: str,
        expected_attestation_sha256: str,
        handler: ExecutorSlotReprobe,
    ) -> bool:
        candidate_digest: str | None = None
        failure_reason = "executor_reprobe_failed"
        try:
            candidate_digest = await asyncio.wait_for(
                handler(socket_path),
                timeout=EXECUTOR_SLOT_REPROBE_TIMEOUT_SECONDS,
            )
            if not _valid_attestation_sha256(candidate_digest):
                candidate_digest = None
                failure_reason = "executor_reprobe_attestation_invalid"
            elif candidate_digest != expected_attestation_sha256:
                failure_reason = "executor_reprobe_attestation_mismatch"
        except asyncio.CancelledError:
            failure_reason = "executor_reprobe_cancelled"
        except Exception as exc:
            failure_reason = (
                "executor_reprobe_failed:" + type(exc).__name__[:80]
            )

        recovered = candidate_digest == expected_attestation_sha256
        async with self._lock:
            slot = next(
                (
                    candidate
                    for candidate in self._slots
                    if candidate.socket_path == socket_path
                ),
                None,
            )
            task = asyncio.current_task()
            if (
                slot is not None
                and slot.reprobe_token == reprobe_token
                and slot.state == "reaping"
            ):
                slot.reprobe_token = None
                if recovered:
                    slot.state = "free"
                    slot.attestation_sha256 = candidate_digest
                    slot.quarantine_reason = None
                    slot.quarantine_recoverable = False
                    slot.reprobe_failure_count = 0
                    slot.reprobe_not_before = 0.0
                else:
                    slot.state = "quarantined"
                    slot.quarantine_reason = failure_reason[:160]
                    slot.reprobe_failure_count += 1
                    if (
                        failure_reason
                        == "executor_reprobe_attestation_mismatch"
                    ):
                        # A valid but heterogeneous runtime may not rejoin the
                        # startup cohort through automatic recovery.
                        slot.attestation_sha256 = candidate_digest
                        slot.quarantine_recoverable = False
                        slot.reprobe_not_before = 0.0
                    else:
                        slot.quarantine_recoverable = True
                        backoff = min(
                            EXECUTOR_SLOT_REPROBE_MAX_BACKOFF_SECONDS,
                            EXECUTOR_SLOT_REPROBE_INITIAL_BACKOFF_SECONDS
                            * (2 ** min(slot.reprobe_failure_count - 1, 4)),
                        )
                        slot.reprobe_not_before = time.monotonic() + backoff
                self._publish_probe_socket_paths_locked()
                self._schedule_locked()
            if (
                task is not None
                and self._reprobe_tasks.get(socket_path) is task
            ):
                self._reprobe_tasks.pop(socket_path, None)
        return recovered

    async def recover_quarantined_slots(
        self,
        *,
        excluded_sockets: frozenset[str] = frozenset(),
        wait_for_result: bool = True,
    ) -> dict[str, int]:
        """Run at most one bounded, cohort-pinned recovery at a time."""

        handler = self._reprobe_handler
        expected_digest = self._homogeneous_attestation_sha256
        if handler is None or expected_digest is None:
            return {"attempted": 0, "recovered": 0}

        async with self._lock:
            tasks = [
                task
                for socket_path, task in self._reprobe_tasks.items()
                if socket_path not in excluded_sockets
            ]
            if not tasks:
                now = time.monotonic()
                candidate = next(
                    (
                        slot
                        for slot in self._slots
                        if slot.state == "quarantined"
                        and slot.quarantine_recoverable
                        and slot.reservation_token is None
                        and slot.reservation_kind is None
                        and slot.reprobe_token is None
                        and slot.socket_path not in excluded_sockets
                        and slot.reprobe_not_before <= now
                    ),
                    None,
                )
                if candidate is not None:
                    self._next_reprobe_token += 1
                    token = (
                        "reprobe-"
                        f"{self._next_reprobe_token}-"
                        f"{candidate.socket_path}"
                    )
                    candidate.state = "reaping"
                    candidate.reprobe_token = token
                    candidate.quarantine_reason = (
                        "executor_reprobe_in_progress"
                    )
                    self._publish_probe_socket_paths_locked()
                    task = asyncio.create_task(
                        self._run_slot_reprobe(
                            socket_path=candidate.socket_path,
                            reprobe_token=token,
                            expected_attestation_sha256=expected_digest,
                            handler=handler,
                        )
                    )
                    self._reprobe_tasks[candidate.socket_path] = task
                    tasks.append(task)
        if not tasks:
            return {"attempted": 0, "recovered": 0}
        if not wait_for_result:
            # ``_reprobe_tasks`` owns the bounded task and
            # ``_run_slot_reprobe`` consumes every outcome. Healthy capacity
            # must not wait behind opportunistic repair of another slot.
            return {"attempted": len(tasks), "recovered": 0}

        joined = asyncio.gather(*tasks)
        try:
            outcomes = await asyncio.shield(joined)
        except asyncio.CancelledError:
            # Runtime-owned re-probe tasks remain bounded and finish their
            # state transition even when this admission caller goes away.
            raise
        return {
            "attempted": len(tasks),
            "recovered": sum(bool(outcome) for outcome in outcomes),
        }

    def _fail_impossible_waiters_locked(self) -> None:
        retained: list[_AdmissionWaiter] = []
        persistent_capacity = self._persistent_limit_locked()
        for waiter in self._waiters:
            if waiter.future.cancelled():
                continue
            if (
                waiter.cancel_event is not None
                and waiter.cancel_event.is_set()
            ):
                waiter.future.set_exception(
                    ExecutorSlotPoolError(
                        "executor_admission_cancelled",
                        "Executor admission was cancelled while waiting for "
                        "a physical slot.",
                    )
                )
                continue
            potentially_usable = any(
                slot.state not in {"quarantined", "unhealthy", "reaping"}
                and slot.socket_path not in waiter.excluded_sockets
                for slot in self._slots
            )
            if not potentially_usable:
                waiter.future.set_exception(
                    ExecutorSlotPoolError(
                        "executor_pool_unavailable",
                        "No healthy executor pool member remains eligible.",
                    )
                )
                continue
            if waiter.kind == "persistent" and persistent_capacity == 0:
                waiter.future.set_exception(
                    ExecutorSlotPoolError(
                        "executor_persistent_capacity_reserved",
                        "The remaining executor capacity is reserved for "
                        "transient work.",
                    )
                )
                continue
            retained.append(waiter)
        self._waiters = retained

    async def _watch_waiter_cancel(
        self,
        waiter: _AdmissionWaiter,
    ) -> None:
        event = waiter.cancel_event
        if event is None:  # pragma: no cover - caller guards this path
            return
        await event.wait()
        async with self._lock:
            if waiter not in self._waiters:
                # The pool-lock grant already won. The caller now owns the
                # reservation and must finish its bounded transaction.
                return
            self._waiters.remove(waiter)
            if not waiter.future.done():
                waiter.future.set_exception(
                    ExecutorSlotPoolError(
                        "executor_admission_cancelled",
                        "Executor admission was cancelled while waiting for "
                        "a physical slot.",
                    )
                )
            self._schedule_locked()

    def _schedule_locked(self) -> None:
        while self._waiters:
            self._fail_impossible_waiters_locked()
            if not self._waiters:
                return
            selected_index: int | None = None
            selected_slot: _ExecutorSlot | None = None
            for index, waiter in enumerate(self._waiters):
                if not self._waiter_is_eligible_locked(waiter):
                    continue
                free_slots = self._ordered_free_slots_locked(
                    waiter.excluded_sockets
                )
                if free_slots:
                    selected_index = index
                    selected_slot = free_slots[0]
                    break
            if selected_index is None or selected_slot is None:
                return
            waiter = self._waiters.pop(selected_index)
            self._next_reservation_token += 1
            token = (
                f"slot-{self._next_reservation_token}-"
                f"{waiter.sequence}"
            )
            selected_slot.state = (
                "leased" if waiter.kind == "persistent" else "reserved"
            )
            selected_slot.reservation_token = token
            selected_slot.reservation_kind = waiter.kind
            selected_slot.quarantine_reason = None
            self._slot_cursor = (
                self._slots.index(selected_slot) + 1
            ) % len(self._slots)
            waiter.future.set_result(
                ExecutorSlotReservation(
                    socket_path=selected_slot.socket_path,
                    kind=waiter.kind,
                    _pool=self,
                    _token=token,
                )
            )

    async def acquire(
        self,
        kind: ReservationKind,
        *,
        excluded_sockets: frozenset[str] = frozenset(),
        cancel_event: asyncio.Event | None = None,
    ) -> ExecutorSlotReservation:
        if kind not in {"transient", "persistent"}:
            raise ExecutorSlotPoolError(
                "executor_pool_admission_invalid",
                "Executor reservation kind is invalid.",
            )
        unknown_exclusions = set(excluded_sockets).difference(
            self.socket_paths
        )
        if unknown_exclusions:
            raise ExecutorSlotPoolError(
                "executor_pool_admission_invalid",
                "Executor admission excludes an unknown pool member.",
            )
        loop = asyncio.get_running_loop()
        if cancel_event is not None and not isinstance(
            cancel_event,
            asyncio.Event,
        ):
            raise ExecutorSlotPoolError(
                "executor_pool_admission_invalid",
                "Executor admission cancel_event must be an asyncio.Event.",
            )
        if cancel_event is not None and cancel_event.is_set():
            raise ExecutorSlotPoolError(
                "executor_admission_cancelled",
                "Executor admission was cancelled while waiting for "
                "a physical slot.",
            )
        async with self._lock:
            must_wait_for_reprobe = not any(
                slot.state
                not in {"quarantined", "unhealthy", "reaping"}
                and slot.socket_path not in excluded_sockets
                for slot in self._slots
            )
        if must_wait_for_reprobe and cancel_event is not None:
            recovery = asyncio.create_task(
                self.recover_quarantined_slots(
                    excluded_sockets=excluded_sockets,
                    wait_for_result=True,
                )
            )
            cancel_waiter = asyncio.create_task(cancel_event.wait())
            try:
                await asyncio.wait(
                    {recovery, cancel_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_event.is_set() and not recovery.done():
                    recovery.cancel()
                    try:
                        await recovery
                    except asyncio.CancelledError:
                        pass
                    raise ExecutorSlotPoolError(
                        "executor_admission_cancelled",
                        "Executor admission was cancelled while waiting for "
                        "a physical slot.",
                    )
                await recovery
            finally:
                cancel_waiter.cancel()
                try:
                    await cancel_waiter
                except asyncio.CancelledError:
                    pass
        else:
            await self.recover_quarantined_slots(
                excluded_sockets=excluded_sockets,
                wait_for_result=must_wait_for_reprobe,
            )
        if cancel_event is not None and cancel_event.is_set():
            raise ExecutorSlotPoolError(
                "executor_admission_cancelled",
                "Executor admission was cancelled while waiting for "
                "a physical slot.",
            )
        future: asyncio.Future[ExecutorSlotReservation] = (
            loop.create_future()
        )
        async with self._lock:
            potentially_usable = [
                slot
                for slot in self._slots
                if slot.state
                not in {"quarantined", "unhealthy", "reaping"}
                and slot.socket_path not in excluded_sockets
            ]
            if not potentially_usable:
                raise ExecutorSlotPoolError(
                    "executor_pool_unavailable",
                    "No healthy executor pool member remains eligible.",
                )
            if (
                kind == "persistent"
                and self._persistent_limit_locked() == 0
            ):
                raise ExecutorSlotPoolError(
                    "executor_persistent_capacity_reserved",
                    "The remaining executor capacity is reserved for "
                    "transient work.",
                )
            self._next_waiter_sequence += 1
            waiter = _AdmissionWaiter(
                sequence=self._next_waiter_sequence,
                kind=kind,
                excluded_sockets=excluded_sockets,
                future=future,
                cancel_event=cancel_event,
            )
            self._waiters.append(waiter)
            self._schedule_locked()
        cancel_watcher = (
            asyncio.create_task(self._watch_waiter_cancel(waiter))
            if cancel_event is not None and not future.done()
            else None
        )
        try:
            return await asyncio.shield(future)
        except BaseException:
            async with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                elif future.done() and not future.cancelled():
                    try:
                        reservation = future.result()
                    except ExecutorSlotPoolError:
                        pass
                    else:
                        try:
                            self._finish_reservation_locked(
                                reservation,
                                quarantine_reason=None,
                            )
                        finally:
                            self._schedule_locked()
                        raise
                self._schedule_locked()
            raise
        finally:
            if cancel_watcher is not None:
                cancel_watcher.cancel()
                try:
                    await cancel_watcher
                except asyncio.CancelledError:
                    pass

    def _finish_reservation_locked(
        self,
        reservation: ExecutorSlotReservation,
        *,
        quarantine_reason: str | None,
    ) -> None:
        matching = next(
            (
                slot
                for slot in self._slots
                if slot.socket_path == reservation.socket_path
            ),
            None,
        )
        if (
            matching is None
            or matching.reservation_token != reservation._token
            or matching.reservation_kind != reservation.kind
        ):
            raise ExecutorSlotPoolError(
                "executor_pool_reservation_mismatch",
                "Executor reservation no longer matches the authoritative "
                "slot state.",
            )
        matching.reservation_token = None
        matching.reservation_kind = None
        if quarantine_reason is None:
            matching.state = "free"
            matching.quarantine_reason = None
            matching.quarantine_recoverable = False
            matching.reprobe_failure_count = 0
            matching.reprobe_not_before = 0.0
        else:
            matching.state = "quarantined"
            matching.quarantine_reason = quarantine_reason
            matching.quarantine_recoverable = (
                quarantine_reason in _RECOVERABLE_QUARANTINE_REASONS
            )
            matching.reprobe_not_before = 0.0
        self._publish_probe_socket_paths_locked()

    async def _finish_reservation(
        self,
        reservation: ExecutorSlotReservation,
        *,
        quarantine_reason: str | None,
    ) -> None:
        start_reprobe = False
        async with self._lock:
            try:
                self._finish_reservation_locked(
                    reservation,
                    quarantine_reason=quarantine_reason,
                )
                start_reprobe = (
                    quarantine_reason
                    in _RECOVERABLE_QUARANTINE_REASONS
                    and self._reprobe_handler is not None
                    and self._homogeneous_attestation_sha256 is not None
                )
            finally:
                self._schedule_locked()
        if start_reprobe:
            # Start cleanup/attestation at the quarantine boundary so a later
            # synchronous Skill preflight is not the component responsible for
            # repairing executor capacity. This call only owns the bounded
            # task; it never waits for recovery or replays the failed request.
            await self.recover_quarantined_slots(
                wait_for_result=False,
            )

    async def apply_startup_attestations(
        self,
        attestations: dict[str, str],
        *,
        failures: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Publish only the largest homogeneous attestation cohort as healthy."""

        unknown = set(attestations).difference(self.socket_paths)
        if unknown or any(
            not _valid_attestation_sha256(digest)
            for digest in attestations.values()
        ):
            raise ExecutorSlotPoolError(
                "executor_attestation_invalid",
                "Startup attestation contains an unknown socket or invalid "
                "SHA-256 digest.",
            )
        counts = Counter(attestations.values())
        largest_count = max(counts.values(), default=0)
        largest_cohort = sorted(
            digest
            for digest, count in counts.items()
            if count == largest_count
        )
        ambiguous = len(largest_cohort) > 1
        winning_digest = (
            largest_cohort[0]
            if len(largest_cohort) == 1
            else None
        )
        failure_map = failures or {}
        async with self._lock:
            if (
                self._waiters
                or self._reprobe_tasks
                or any(
                    slot.reservation_token is not None
                    or slot.state == "reaping"
                    for slot in self._slots
                )
            ):
                raise ExecutorSlotPoolError(
                    "executor_pool_startup_race",
                    "Executor startup attestation raced active admission.",
                )
            healthy: list[str] = []
            quarantined: list[str] = []
            self._homogeneous_attestation_sha256 = (
                None if ambiguous else winning_digest
            )
            for slot in self._slots:
                digest = attestations.get(slot.socket_path)
                slot.reservation_token = None
                slot.reservation_kind = None
                slot.reprobe_token = None
                slot.reprobe_failure_count = 0
                slot.reprobe_not_before = 0.0
                slot.attestation_sha256 = digest
                if (
                    not ambiguous
                    and digest is not None
                    and digest == winning_digest
                ):
                    slot.state = "free"
                    slot.quarantine_reason = None
                    slot.quarantine_recoverable = False
                    healthy.append(slot.socket_path)
                else:
                    slot.state = "quarantined"
                    slot.quarantine_reason = (
                        str(failure_map.get(slot.socket_path) or "")
                        or "executor_attestation_mismatch"
                    )[:160]
                    slot.quarantine_recoverable = False
                    quarantined.append(slot.socket_path)
            self._publish_probe_socket_paths_locked()
            self._schedule_locked()
            if ambiguous:
                raise ExecutorSlotPoolError(
                    "executor_attestation_ambiguous",
                    "Executor startup found multiple equally sized runtime "
                    "attestation cohorts.",
                )
        return {
            "configured_count": len(self._slots),
            "healthy_count": len(healthy),
            "quarantined_count": len(quarantined),
            "attestation_sha256": winning_digest,
            "healthy_sockets": tuple(healthy),
            "quarantined_sockets": tuple(quarantined),
        }

    async def snapshot(self) -> tuple[dict[str, object], ...]:
        """Return a trusted diagnostic projection; never expose it to tools."""

        async with self._lock:
            return tuple(
                {
                    "socket_path": slot.socket_path,
                    "state": slot.state,
                    "reservation_kind": slot.reservation_kind,
                    "attestation_sha256": slot.attestation_sha256,
                    "quarantine_reason": slot.quarantine_reason,
                    "quarantine_recoverable": (
                        slot.quarantine_recoverable
                    ),
                    "reprobe_failure_count": slot.reprobe_failure_count,
                }
                for slot in self._slots
            )


_POOL_REGISTRY: dict[tuple[str, ...], ExecutorSlotPool] = {}
_POOL_REGISTRY_LOCK = threading.Lock()


def get_executor_slot_pool(
    *,
    primary_socket: str | None = None,
) -> ExecutorSlotPool:
    """Get the process-wide admission pool for the exact socket topology."""

    paths = configured_executor_socket_paths(primary_socket=primary_socket)
    with _POOL_REGISTRY_LOCK:
        pool = _POOL_REGISTRY.get(paths)
        if pool is None:
            pool = ExecutorSlotPool(paths)
            _POOL_REGISTRY[paths] = pool
        return pool


def reset_executor_slot_pool_registry_for_tests() -> None:
    """Forget lazy pools. Production code must never call this helper."""

    with _POOL_REGISTRY_LOCK:
        _POOL_REGISTRY.clear()
