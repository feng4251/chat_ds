"""Provider-scoped admission for bounded concurrent model requests.

The provider's own queue protects its process, but queueing after an HTTP
request has been opened consumes the harness' transport deadline.  This module
keeps that wait on the harness side and admits requests by both count and a
conservative estimated-token weight.

Admission is deliberately independent of Skills, workflow stages, and model
semantics.  State is scoped to the running event loop so test-loop teardown,
uvicorn reloads, and future embedded runtimes cannot reuse loop-bound asyncio
primitives.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import threading
import weakref
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit


logger = logging.getLogger(__name__)

AdmissionObserver = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ProviderKey = tuple[str, str]


class ProviderAdmissionTimeout(TimeoutError):
    """A request could not enter its provider budget before the wait bound."""


@dataclass(frozen=True)
class ProviderAdmissionLimits:
    """Deployment-level limits for one normalized provider/model identity.

    Zero disables the corresponding limit.  If both limits are zero,
    admission is a compatibility no-op.  The safety factor is applied only to
    estimated input tokens; the already-clamped maximum output is reserved in
    full.
    """

    max_inflight_requests: int = 0
    max_inflight_estimated_tokens: int = 0
    estimate_safety_factor: float = 1.0
    wait_timeout_seconds: float = 0.0

    def normalized(self) -> "ProviderAdmissionLimits":
        try:
            request_limit = int(self.max_inflight_requests)
        except (TypeError, ValueError, OverflowError):
            request_limit = 0
        try:
            token_limit = int(self.max_inflight_estimated_tokens)
        except (TypeError, ValueError, OverflowError):
            token_limit = 0
        try:
            factor = float(self.estimate_safety_factor)
        except (TypeError, ValueError, OverflowError):
            factor = 1.0
        try:
            wait_timeout = float(self.wait_timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            wait_timeout = 0.0
        if not math.isfinite(factor):
            factor = 1.0
        if not math.isfinite(wait_timeout):
            wait_timeout = 0.0
        return ProviderAdmissionLimits(
            max_inflight_requests=max(0, request_limit),
            max_inflight_estimated_tokens=max(0, token_limit),
            # A deployment configuration must not make an input estimate less
            # conservative than the shared token estimator itself.
            estimate_safety_factor=max(1.0, factor),
            wait_timeout_seconds=max(0.0, wait_timeout),
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.max_inflight_requests > 0
            or self.max_inflight_estimated_tokens > 0
        )


@dataclass(frozen=True)
class ProviderIdentity:
    """Internal normalized provider identity plus its safe debug digest."""

    key: ProviderKey
    debug_hash: str


@dataclass
class _Waiter:
    ticket: int
    weight: int
    enqueued_at: float


class _ProviderState:
    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.active_requests = 0
        self.active_tokens = 0
        self.waiters: deque[_Waiter] = deque()
        self.next_ticket = 1


def _normalized_endpoint(endpoint: str) -> str:
    """Normalize routing identity while dropping credentials and query data."""

    raw = str(endpoint or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        # This fallback is intentionally lossy: provider identities need only
        # remain stable within the process, and credentials/query fragments
        # must never survive normalization.
        return raw.split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1].rstrip("/")

    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = (parsed.path or "").rstrip("/")
    if not scheme and not host:
        # A non-URL endpoint is still stripped of the common secret-bearing
        # components before it becomes an internal key.
        return path.rsplit("@", 1)[-1]
    return urlunsplit((scheme, host, path, "", ""))


def provider_identity(endpoint: str, api_model: str) -> ProviderIdentity:
    normalized_endpoint = _normalized_endpoint(endpoint)
    normalized_model = str(api_model or "").strip()
    key = (normalized_endpoint, normalized_model)
    digest_input = json.dumps(
        [normalized_endpoint, normalized_model],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return ProviderIdentity(
        key=key,
        debug_hash=hashlib.sha256(digest_input).hexdigest()[:20],
    )


def estimate_admission_tokens(
    estimated_input_tokens: int,
    max_output_tokens: int,
    *,
    safety_factor: float,
) -> int:
    """Return the conservative KV-like weight reserved for one request."""

    try:
        input_tokens = max(1, int(estimated_input_tokens))
    except (TypeError, ValueError, OverflowError):
        input_tokens = 1
    try:
        output_tokens = max(0, int(max_output_tokens))
    except (TypeError, ValueError, OverflowError):
        output_tokens = 0
    try:
        factor = float(safety_factor)
    except (TypeError, ValueError, OverflowError):
        factor = 1.0
    if not math.isfinite(factor):
        factor = 1.0
    try:
        weighted_input = int(
            (Decimal(input_tokens) * Decimal(str(max(1.0, factor)))).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
    except (InvalidOperation, ValueError, OverflowError):
        weighted_input = input_tokens
    return max(1, weighted_input + output_tokens)


class ProviderAdmissionLease:
    """One acquired provider slot; release is idempotent and exception-safe."""

    def __init__(
        self,
        *,
        controller: "ProviderAdmissionController",
        loop: asyncio.AbstractEventLoop,
        identity: ProviderIdentity,
        state: _ProviderState | None,
        limits: ProviderAdmissionLimits,
        weight: int,
        ticket: int,
        wait_seconds: float,
        acquired_at: float,
        observer: AdmissionObserver | None,
    ) -> None:
        self._controller = controller
        self._loop = loop
        self._identity = identity
        self._state = state
        self._limits = limits
        self._weight = weight
        self._ticket = ticket
        self._wait_seconds = wait_seconds
        self._acquired_at = acquired_at
        self._observer = observer
        self._released = False

    @property
    def enabled(self) -> bool:
        return self._state is not None

    async def release(self) -> None:
        if self._released:
            return
        if self._state is None:
            self._released = True
            return
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("provider admission lease released on a different event loop")
        self._released = True
        payload = await self._controller._release(
            identity=self._identity,
            state=self._state,
            limits=self._limits,
            weight=self._weight,
            ticket=self._ticket,
            wait_seconds=self._wait_seconds,
            acquired_at=self._acquired_at,
        )
        await _emit_observer(self._observer, "released", payload)

    async def __aenter__(self) -> "ProviderAdmissionLease":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release()


class ProviderAdmissionController:
    """FIFO weighted admission registry, isolated by loop and provider key."""

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._states_by_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop,
            dict[ProviderKey, _ProviderState],
        ] = weakref.WeakKeyDictionary()

    def _state_for(
        self,
        loop: asyncio.AbstractEventLoop,
        key: ProviderKey,
    ) -> _ProviderState:
        with self._registry_lock:
            states = self._states_by_loop.setdefault(loop, {})
            state = states.get(key)
            if state is None:
                state = _ProviderState()
                states[key] = state
            return state

    async def acquire(
        self,
        *,
        endpoint: str,
        api_model: str,
        estimated_input_tokens: int,
        max_output_tokens: int,
        limits: ProviderAdmissionLimits,
        observer: AdmissionObserver | None = None,
    ) -> ProviderAdmissionLease:
        normalized_limits = limits.normalized()
        identity = provider_identity(endpoint, api_model)
        weight = estimate_admission_tokens(
            estimated_input_tokens,
            max_output_tokens,
            safety_factor=normalized_limits.estimate_safety_factor,
        )
        loop = asyncio.get_running_loop()
        if not normalized_limits.enabled:
            return ProviderAdmissionLease(
                controller=self,
                loop=loop,
                identity=identity,
                state=None,
                limits=normalized_limits,
                weight=weight,
                ticket=0,
                wait_seconds=0.0,
                acquired_at=loop.time(),
                observer=observer,
            )

        state = self._state_for(loop, identity.key)
        async with state.condition:
            waiter = _Waiter(
                ticket=state.next_ticket,
                weight=weight,
                enqueued_at=loop.time(),
            )
            state.next_ticket += 1
            state.waiters.append(waiter)
            queued_payload = _snapshot(
                identity,
                state,
                normalized_limits,
                waiter=waiter,
                wait_seconds=0.0,
            )

        admitted = False
        lease: ProviderAdmissionLease | None = None
        try:
            await _emit_observer(observer, "queued", queued_payload)
            deadline = (
                waiter.enqueued_at + normalized_limits.wait_timeout_seconds
                if normalized_limits.wait_timeout_seconds > 0
                else None
            )
            async with state.condition:
                while not _can_admit(state, waiter, normalized_limits):
                    if deadline is None:
                        await state.condition.wait()
                        continue
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise ProviderAdmissionTimeout(
                            "provider admission wait timeout"
                        )
                    try:
                        await asyncio.wait_for(
                            state.condition.wait(),
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError as exc:
                        raise ProviderAdmissionTimeout(
                            "provider admission wait timeout"
                        ) from exc

                # FIFO is authoritative: only the head can pass _can_admit.
                popped = state.waiters.popleft()
                if popped is not waiter:
                    raise RuntimeError("provider admission FIFO state corruption")
                state.active_requests += 1
                state.active_tokens += weight
                admitted = True
                acquired_at = loop.time()
                wait_seconds = max(0.0, acquired_at - waiter.enqueued_at)
                acquired_payload = _snapshot(
                    identity,
                    state,
                    normalized_limits,
                    waiter=waiter,
                    wait_seconds=wait_seconds,
                )
                state.condition.notify_all()

            lease = ProviderAdmissionLease(
                controller=self,
                loop=loop,
                identity=identity,
                state=state,
                limits=normalized_limits,
                weight=weight,
                ticket=waiter.ticket,
                wait_seconds=wait_seconds,
                acquired_at=acquired_at,
                observer=observer,
            )
            try:
                await _emit_observer(observer, "acquired", acquired_payload)
            except BaseException:
                # Debug/event cancellation must never strand provider capacity.
                await lease.release()
                raise
            return lease
        except BaseException as exc:
            if not admitted:
                await self._remove_waiter(state, waiter)
                if isinstance(exc, ProviderAdmissionTimeout):
                    timeout_payload = dict(queued_payload)
                    timeout_payload.update({
                        "queued_requests": len(state.waiters),
                        "queue_position": 0,
                        "wait_ms": max(
                            0,
                            round((loop.time() - waiter.enqueued_at) * 1000),
                        ),
                        "wait_timeout_ms": max(
                            0,
                            round(
                                normalized_limits.wait_timeout_seconds * 1000
                            ),
                        ),
                    })
                    await _emit_observer(observer, "timed_out", timeout_payload)
            raise

    async def _remove_waiter(
        self,
        state: _ProviderState,
        waiter: _Waiter,
    ) -> None:
        async with state.condition:
            try:
                state.waiters.remove(waiter)
            except ValueError:
                return
            state.condition.notify_all()

    async def _release(
        self,
        *,
        identity: ProviderIdentity,
        state: _ProviderState,
        limits: ProviderAdmissionLimits,
        weight: int,
        ticket: int,
        wait_seconds: float,
        acquired_at: float,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        async with state.condition:
            state.active_requests = max(0, state.active_requests - 1)
            state.active_tokens = max(0, state.active_tokens - weight)
            payload = _snapshot(
                identity,
                state,
                limits,
                waiter=_Waiter(ticket, weight, acquired_at - wait_seconds),
                wait_seconds=wait_seconds,
            )
            payload["held_ms"] = max(0, round((loop.time() - acquired_at) * 1000))
            state.condition.notify_all()
            return payload


def _can_admit(
    state: _ProviderState,
    waiter: _Waiter,
    limits: ProviderAdmissionLimits,
) -> bool:
    if not state.waiters or state.waiters[0] is not waiter:
        return False
    if (
        limits.max_inflight_requests > 0
        and state.active_requests >= limits.max_inflight_requests
    ):
        return False
    if limits.max_inflight_estimated_tokens <= 0:
        return True
    if waiter.weight > limits.max_inflight_estimated_tokens:
        # A valid single request may be larger than the deployment's preferred
        # aggregate budget.  Let it make progress only in complete isolation.
        return state.active_requests == 0 and state.active_tokens == 0
    return (
        state.active_tokens + waiter.weight
        <= limits.max_inflight_estimated_tokens
    )


def _snapshot(
    identity: ProviderIdentity,
    state: _ProviderState,
    limits: ProviderAdmissionLimits,
    *,
    waiter: _Waiter,
    wait_seconds: float,
) -> dict[str, Any]:
    try:
        queue_position = list(state.waiters).index(waiter) + 1
    except ValueError:
        queue_position = 0
    return {
        "provider_key_sha256": identity.debug_hash,
        "ticket": waiter.ticket,
        "request_estimated_tokens": waiter.weight,
        "max_inflight_requests": limits.max_inflight_requests,
        "max_inflight_estimated_tokens": (
            limits.max_inflight_estimated_tokens
        ),
        "estimate_safety_factor": limits.estimate_safety_factor,
        "active_requests": state.active_requests,
        "active_estimated_tokens": state.active_tokens,
        "queued_requests": len(state.waiters),
        "queue_position": queue_position,
        "oversize_exclusive": bool(
            limits.max_inflight_estimated_tokens > 0
            and waiter.weight > limits.max_inflight_estimated_tokens
        ),
        "wait_ms": max(0, round(wait_seconds * 1000)),
    }


async def _emit_observer(
    observer: AdmissionObserver | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if observer is None:
        return
    try:
        result = observer(event_type, dict(payload))
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Provider admission observer failed for %s", event_type)


provider_admission = ProviderAdmissionController()


__all__ = [
    "AdmissionObserver",
    "ProviderAdmissionController",
    "ProviderAdmissionLease",
    "ProviderAdmissionLimits",
    "ProviderAdmissionTimeout",
    "ProviderIdentity",
    "estimate_admission_tokens",
    "provider_admission",
    "provider_identity",
]
