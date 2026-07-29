"""Runtime-owned revocable authority for one delegated execution.

Task cancellation is only a scheduling signal: an asyncio task may catch or
delay ``CancelledError``.  A delegated child therefore receives a separate
capability fence.  Revocation changes the fence generation synchronously, so
every later dispatch/commit check fails even when the child keeps running.

The fence also owns bounded resource-close callbacks.  Teardown never awaits
an uncooperative callback or task without a deadline; residual tasks are kept
under process-local supervision until they eventually finish.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


class ExecutionAuthorityRevoked(RuntimeError):
    """Raised before a revoked child can cross an execution boundary."""

    code = "execution_authority_revoked"


ResourceCloser = Callable[[], Awaitable[Any]]


@dataclass(slots=True)
class _RegisteredResource:
    """One retained closer and its at-most-one runtime-owned attempt."""

    label: str
    closer: ResourceCloser
    close_task: asyncio.Task[Any] | None = None


@dataclass(frozen=True, slots=True)
class FenceTeardownReport:
    fence_id: str
    revoked: bool
    generation: int
    resource_count: int
    acknowledged_resource_count: int
    unacknowledged_resource_count: int
    cancellation_unacknowledged: bool
    fence_coverage_proven: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "fence_id": self.fence_id,
            "revoked": self.revoked,
            "generation": self.generation,
            "resource_count": self.resource_count,
            "acknowledged_resource_count": self.acknowledged_resource_count,
            "unacknowledged_resource_count": (
                self.unacknowledged_resource_count
            ),
            "cancellation_unacknowledged": (
                self.cancellation_unacknowledged
            ),
            "fence_coverage_proven": self.fence_coverage_proven,
        }


_SUPERVISED_RESIDUAL_TASKS: set[asyncio.Task[Any]] = set()


def supervise_residual_task(task: asyncio.Task[Any]) -> None:
    """Retain and consume one task which outlived a bounded teardown."""

    _SUPERVISED_RESIDUAL_TASKS.add(task)

    def finished(done: asyncio.Task[Any]) -> None:
        _SUPERVISED_RESIDUAL_TASKS.discard(done)
        try:
            done.exception()
        except BaseException:
            pass

    task.add_done_callback(finished)


async def bounded_cancel_tasks(
    tasks: set[asyncio.Task[Any]],
    *,
    grace_seconds: float,
) -> set[asyncio.Task[Any]]:
    """Cancel tasks and wait at most ``grace_seconds`` for acknowledgement.

    Pending tasks remain supervised, but their execution authority must
    already have been revoked by the caller before this helper is used.
    """

    pending = {task for task in tasks if not task.done()}
    for task in pending:
        task.cancel()
    if not pending:
        return set()
    done, residual = await asyncio.wait(
        pending,
        timeout=max(0.001, float(grace_seconds)),
        return_when=asyncio.ALL_COMPLETED,
    )
    for task in done:
        try:
            task.exception()
        except BaseException:
            pass
    for task in residual:
        supervise_residual_task(task)
    return set(residual)


class ChildExecutionFence:
    """A generation-bound, runtime-owned child execution capability."""

    def __init__(self) -> None:
        self._fence_id = uuid.uuid4().hex
        self._generation = 1
        self._revoked = False
        self._reason = ""
        self._lock = threading.Lock()
        self._resources: dict[str, _RegisteredResource] = {}

    @property
    def fence_id(self) -> str:
        return self._fence_id

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def revoked(self) -> bool:
        with self._lock:
            return self._revoked

    def require(self, expected_generation: int, *, boundary: str) -> None:
        """Prove that one dispatch/commit still owns the original generation."""

        with self._lock:
            allowed = (
                not self._revoked
                and isinstance(expected_generation, int)
                and not isinstance(expected_generation, bool)
                and expected_generation == self._generation
            )
            reason = self._reason
        if allowed:
            return
        detail = "delegated execution authority was revoked"
        if reason:
            detail += f" ({reason})"
        raise ExecutionAuthorityRevoked(
            f"{detail}; boundary={str(boundary or 'unspecified')[:120]}"
        )

    def register_resource(
        self,
        expected_generation: int,
        *,
        label: str,
        closer: ResourceCloser,
    ) -> str:
        """Register a child-owned resource while the generation is active."""

        if not callable(closer):
            raise TypeError("resource closer must be callable")
        async_callable = inspect.iscoroutinefunction(closer) or (
            hasattr(closer, "__call__")
            and inspect.iscoroutinefunction(closer.__call__)
        )
        if not async_callable:
            # Calling a synchronous closer can block the event loop before an
            # asyncio timeout has any opportunity to fire. Runtime-owned
            # teardown therefore accepts only coroutine functions; adapters
            # must expose bounded asynchronous cleanup explicitly.
            raise TypeError(
                "resource closer must be an async callable"
            )
        token = uuid.uuid4().hex
        with self._lock:
            if (
                self._revoked
                or expected_generation != self._generation
            ):
                raise ExecutionAuthorityRevoked(
                    "delegated execution authority was revoked before "
                    "resource registration"
                )
            self._resources[token] = _RegisteredResource(
                label=str(label or "resource")[:120],
                closer=closer,
            )
        return token

    def unregister_resource(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._resources.pop(str(token), None)

    def revoke(self, reason: str) -> int:
        """Atomically revoke all contexts bound to the current generation."""

        with self._lock:
            if not self._revoked:
                self._revoked = True
                self._reason = str(reason or "runtime_cancelled")[:160]
                self._generation += 1
            return self._generation

    async def close_registered_resources(
        self,
        *,
        grace_seconds: float,
    ) -> FenceTeardownReport:
        """Close resources once, retaining every unacknowledged closer.

        Concurrent callers join the same runtime-owned task for each token.
        Only an acknowledged successful return removes a token.  Exceptions,
        acknowledged cancellation after a timeout, and synchronous invocation
        failures clear the attempt but retain the exact closer for retry.
        An uncooperative timed-out attempt remains the single in-flight task,
        preventing a retry from running two closers concurrently.
        """

        with self._lock:
            resources = list(self._resources.items())
            generation = self._generation
            revoked = self._revoked
            for _token, resource in resources:
                previous = resource.close_task
                if (
                    previous is not None
                    and previous.done()
                    and (
                        previous.cancelled()
                        or (
                            previous.exception()
                            is not None
                        )
                    )
                ):
                    # A prior caller may itself have been cancelled before it
                    # could release a terminal failed attempt. Retry it here
                    # without requiring an otherwise spurious third close.
                    resource.close_task = None
                if resource.close_task is None:
                    async def invoke(
                        closer: ResourceCloser = resource.closer,
                    ) -> Any:
                        value = closer()
                        if not inspect.isawaitable(value):
                            raise TypeError(
                                "resource closer returned a non-awaitable"
                            )
                        return await value

                    resource.close_task = asyncio.create_task(invoke())
                    resource.close_task.add_done_callback(
                        lambda done: (
                            done.exception()
                            if not done.cancelled()
                            else None
                        )
                    )
            task_rows = [
                (token, resource, resource.close_task)
                for token, resource in resources
                if resource.close_task is not None
            ]

        tasks = {
            task
            for _token, _resource, task in task_rows
        }
        done: set[asyncio.Task[Any]] = set()
        pending: set[asyncio.Task[Any]] = set()
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=max(0.001, float(grace_seconds)),
                return_when=asyncio.ALL_COMPLETED,
            )

        # Timeout is not acknowledgement. Request cancellation so a
        # cooperative closer can be retried, but never overlap a closer that
        # ignores cancellation.
        for task in pending:
            task.cancel()
        residual: set[asyncio.Task[Any]] = set()
        if pending:
            cancelled_done, residual = await asyncio.wait(
                pending,
                timeout=0.001,
                return_when=asyncio.ALL_COMPLETED,
            )
            done.update(cancelled_done)
            for task in residual:
                supervise_residual_task(task)

        acknowledged = 0
        for token, resource, task in task_rows:
            successful = False
            if task.done() and not task.cancelled():
                try:
                    successful = task.exception() is None
                except BaseException:
                    successful = False
            with self._lock:
                current = self._resources.get(token)
                if current is not resource:
                    # Explicit unregistration is an independent acknowledgement
                    # owned by the adapter. It must not let this close call
                    # delete or mutate a newly registered token.
                    acknowledged += 1
                    continue
                if successful:
                    self._resources.pop(token, None)
                    acknowledged += 1
                elif (
                    task.done()
                    and resource.close_task is task
                ):
                    # Failed/cooperatively cancelled attempts are retryable.
                    # Preserve the exact closer but release this terminal task.
                    resource.close_task = None

        unacknowledged = max(0, len(resources) - acknowledged)
        return FenceTeardownReport(
            fence_id=self._fence_id,
            revoked=revoked,
            generation=generation,
            resource_count=len(resources),
            acknowledged_resource_count=acknowledged,
            unacknowledged_resource_count=unacknowledged,
            cancellation_unacknowledged=bool(unacknowledged),
            fence_coverage_proven=revoked,
        )


def require_execution_authority(
    context: Any | None,
    *,
    boundary: str,
) -> None:
    """Check a ToolContext fence when the runtime attached one."""

    if context is None:
        return
    fence = getattr(context, "execution_fence", None)
    generation = getattr(context, "execution_fence_generation", None)
    if fence is None:
        # Primary and direct compatibility contexts predate delegated fences.
        # Production children are created only by tools.delegation, which
        # always binds one before _run_child can execute.
        return
    if not isinstance(fence, ChildExecutionFence):
        raise ExecutionAuthorityRevoked(
            "delegated ToolContext carries an invalid execution authority"
        )
    fence.require(generation, boundary=boundary)


def register_execution_resource(
    context: Any | None,
    *,
    label: str,
    closer: ResourceCloser,
) -> str | None:
    """Register a close callback on the context's active fence, if present."""

    if context is None:
        return None
    fence = getattr(context, "execution_fence", None)
    generation = getattr(context, "execution_fence_generation", None)
    if fence is None:
        return None
    if not isinstance(fence, ChildExecutionFence):
        raise ExecutionAuthorityRevoked(
            "delegated ToolContext carries an invalid execution authority"
        )
    return fence.register_resource(
        generation,
        label=label,
        closer=closer,
    )


def unregister_execution_resource(
    context: Any | None,
    token: str | None,
) -> None:
    if context is None or not token:
        return
    fence = getattr(context, "execution_fence", None)
    if isinstance(fence, ChildExecutionFence):
        fence.unregister_resource(token)
