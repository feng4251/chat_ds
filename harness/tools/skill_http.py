"""Restricted HTTP bridges for literal endpoints declared by session Skills."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import socket
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

import aiohttp

from config import settings
from retrieval_completeness import build_http_retrieval_receipt
from skills.http_grants import (
    canonical_https_prefix,
    canonical_https_request_url,
)
from tools.context import ToolContext
from tools.execution_fence import (
    ExecutionAuthorityRevoked,
    require_execution_authority,
)
from tools.tool_result_storage import (
    MAX_LOSSLESS_SPILL_BYTES,
    persist_tool_result_spill,
)


DEFAULT_MAX_CHARS = 40_000
MAX_CHARS = 100_000
DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 30
# Keep the complete-wire capture ceiling coherent with the lossless spill
# store.  ``max_chars`` remains the much smaller model-visible presentation
# limit; increasing this producer ceiling therefore does not inflate context.
# Responses above the spill safety limit still fail closed as partial wire.
MAX_RESPONSE_BYTES = MAX_LOSSLESS_SPILL_BYTES
MAX_REDIRECTS = 3
MAX_JSON_BODY_BYTES = 64_000
MAX_JSON_BODY_DEPTH = 20
MAX_JSON_BODY_NODES = 4_096
# Quotas count actual network hops, including redirects and failed DNS/connect
# attempts. A child cannot reset the enclosing workflow's root quota merely by
# spawning another delegated run. The configurable default supports legitimate
# serial evidence pagination while an immutable hard ceiling remains in code.
DEFAULT_MAX_REQUESTS_PER_RUN = 16
HARD_MAX_REQUESTS_PER_RUN = 32
# Compatibility/export for tests and callers that need the shipped default.
MAX_REQUESTS_PER_RUN = DEFAULT_MAX_REQUESTS_PER_RUN
MAX_REQUESTS_PER_ROOT_RUN = 64
MAX_REQUESTS_PER_USER_WINDOW = 128
USER_REQUEST_WINDOW_SECONDS = 60.0
MAX_TRACKED_RUNS = 10_000
MAX_TRACKED_USERS = 10_000
MAX_CONCURRENT_REQUESTS = 24
MAX_CONCURRENT_PER_HOST = 4
MAX_CONCURRENT_PER_ROOT_RUN = 8
MAX_CONCURRENT_PER_USER = 8
MAX_DNS_WORKERS = 8
_SENSITIVE_QUERY_KEYS = frozenset({
    "apikey", "accesstoken", "refreshtoken", "idtoken", "token",
    "secret", "clientsecret", "password", "passwd", "key", "xapikey",
    "authorization", "auth", "bearer", "credential", "signature", "sig",
    "cookie", "setcookie", "session", "sessionid", "csrftoken", "privatekey",
})
_request_counts: OrderedDict[str, int] = OrderedDict()
_root_request_counts: OrderedDict[str, int] = OrderedDict()
_user_request_windows: OrderedDict[str, deque[float]] = OrderedDict()
_active_request_total = 0
_active_by_host: dict[str, int] = {}
_active_by_root: dict[str, int] = {}
_active_by_user: dict[str, int] = {}
# State mutation contains no await and is constant-time. A threading lock is
# intentionally used so test servers/reloaders that create a new event loop do
# not retain an asyncio.Lock bound to a dead loop.
_request_count_lock = threading.Lock()
# ``getaddrinfo`` cancellation cannot stop a resolver thread. Isolate it in a
# small dedicated pool so a DNS outage cannot exhaust the harness's shared
# executor even after callers hit their total deadline.
_DNS_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_DNS_WORKERS,
    thread_name_prefix="skill-http-dns",
)
_DNS_SLOTS = threading.BoundedSemaphore(MAX_DNS_WORKERS)
_GET_TRANSPORT_RETRY_DEPTH: ContextVar[int] = ContextVar(
    "skill_http_get_transport_retry_depth",
    default=0,
)


async def _read_bounded_response_bytes(response: Any) -> tuple[bytes, bool]:
    """Capture one response completely up to the lossless spill ceiling.

    The returned boolean is true only when at least one wire byte could not be
    retained.  GET and POST deliberately share this producer boundary so one
    bridge cannot silently provide weaker completeness semantics than the
    other.
    """

    chunks: list[bytes] = []
    size = 0
    truncated = False
    async for chunk in response.content.iter_chunked(16_384):
        remaining = MAX_RESPONSE_BYTES - size
        if remaining <= 0:
            truncated = True
            break
        retained = chunk[:remaining]
        chunks.append(retained)
        size += len(retained)
        if len(chunk) > remaining:
            truncated = True
            break
    return b"".join(chunks), truncated


def _configured_max_requests_per_run() -> int:
    try:
        configured = int(settings.skill_http_max_requests_per_run)
    except (TypeError, ValueError, OverflowError):
        configured = DEFAULT_MAX_REQUESTS_PER_RUN
    return max(1, min(HARD_MAX_REQUESTS_PER_RUN, configured))


@dataclass(frozen=True)
class _RequestLease:
    host: str
    root_identity: str
    user_identity: str
    request_number: int
    root_request_number: int


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Resolve one authorized hostname only to prevalidated public addresses."""

    def __init__(self, hostname: str, addresses: tuple[tuple[str, int], ...]):
        self.hostname = hostname.casefold()
        self.addresses = addresses

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        if host.rstrip(".").casefold() != self.hostname:
            raise OSError("redirect hostname is outside the pinned resolver")
        records = []
        for address, address_family in self.addresses:
            if family not in (socket.AF_UNSPEC, address_family):
                continue
            records.append({
                "hostname": host,
                "host": address,
                "port": port,
                "family": address_family,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            })
        if not records:
            raise OSError("authorized hostname has no address for the requested family")
        return records

    async def close(self) -> None:
        return None


def _error(code: str, message: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "error_code": code, "error": message, **extra}, ensure_ascii=False)


def _canonical_request_url(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None, "url must be one non-empty HTTPS string without surrounding whitespace"
    canonical = canonical_https_request_url(value)
    if canonical is None:
        return None, (
            "url must be a strict credential-free HTTPS URL on the default "
            "port with no fragment, whitespace, traversal, repeated/raw "
            "separator, or dangerous percent encoding"
        )
    try:
        query_items = parse_qsl(
            urlsplit(canonical).query,
            keep_blank_values=True,
            max_num_fields=256,
        )
    except ValueError:
        return None, "url query exceeds the bounded field policy"
    for key, value_part in query_items:
        normalized_key = re.sub(r"[^a-z0-9]+", "", key.casefold())
        if normalized_key in _SENSITIVE_QUERY_KEYS and value_part:
            return None, "credential-bearing query parameters are forbidden"
    return canonical, None


def _json_body_bytes(value: Any) -> tuple[bytes | None, str | None]:
    """Validate and encode one bounded, credential-free JSON object.

    The public schema catches ordinary shape errors, while this runtime check
    remains authoritative for direct/custom callers and for JSON decoders that
    accept non-finite numbers.  Iterative traversal avoids recursive-parser
    exhaustion and rejects cyclic/non-JSON Python values before serialization.
    """

    if not isinstance(value, dict):
        return None, "body must be one JSON object"

    nodes = 0
    string_bytes = 0
    active: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 1, False)]
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.discard(id(item))
            continue
        nodes += 1
        if nodes > MAX_JSON_BODY_NODES:
            return None, (
                f"body exceeds the {MAX_JSON_BODY_NODES}-node JSON limit"
            )
        if depth > MAX_JSON_BODY_DEPTH:
            return None, (
                f"body exceeds the {MAX_JSON_BODY_DEPTH}-level JSON depth limit"
            )

        if isinstance(item, dict):
            identity = id(item)
            if identity in active:
                return None, "body must not contain cyclic JSON values"
            active.add(identity)
            stack.append((item, depth, True))
            for key, child in reversed(tuple(item.items())):
                if not isinstance(key, str):
                    return None, "body object keys must be strings"
                if len(key) > MAX_JSON_BODY_BYTES:
                    return None, (
                        f"body exceeds the {MAX_JSON_BODY_BYTES}-byte encoded JSON limit"
                    )
                try:
                    string_bytes += len(key.encode("utf-8"))
                except UnicodeError:
                    return None, "body contains text that is not valid UTF-8"
                if string_bytes > MAX_JSON_BODY_BYTES:
                    return None, (
                        f"body exceeds the {MAX_JSON_BODY_BYTES}-byte encoded JSON limit"
                    )
                normalized_key = re.sub(r"[^a-z0-9]+", "", key.casefold())
                if normalized_key in _SENSITIVE_QUERY_KEYS and child not in (
                    None, "",
                ):
                    return None, (
                        "credential-bearing JSON properties are forbidden"
                    )
                # Count the key as JSON structure as well as its value. This
                # keeps a very wide object from bypassing the node budget.
                nodes += 1
                if nodes > MAX_JSON_BODY_NODES:
                    return None, (
                        f"body exceeds the {MAX_JSON_BODY_NODES}-node JSON limit"
                    )
                stack.append((child, depth + 1, False))
            continue
        if isinstance(item, list):
            identity = id(item)
            if identity in active:
                return None, "body must not contain cyclic JSON values"
            active.add(identity)
            stack.append((item, depth, True))
            for child in reversed(item):
                stack.append((child, depth + 1, False))
            continue
        if isinstance(item, str):
            if len(item) > MAX_JSON_BODY_BYTES:
                return None, (
                    f"body exceeds the {MAX_JSON_BODY_BYTES}-byte encoded JSON limit"
                )
            try:
                string_bytes += len(item.encode("utf-8"))
            except UnicodeError:
                return None, "body contains text that is not valid UTF-8"
            if string_bytes > MAX_JSON_BODY_BYTES:
                return None, (
                    f"body exceeds the {MAX_JSON_BODY_BYTES}-byte encoded JSON limit"
                )
            continue
        if item is None or isinstance(item, (bool, int)):
            continue
        if isinstance(item, float):
            if math.isfinite(item):
                continue
            return None, "body must contain only finite JSON numbers"
        return None, (
            "body contains a non-JSON value of type "
            f"{type(item).__name__}"
        )

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        return None, f"body is not valid JSON ({type(exc).__name__})"
    if len(encoded) > MAX_JSON_BODY_BYTES:
        return None, (
            f"body exceeds the {MAX_JSON_BODY_BYTES}-byte encoded JSON limit"
        )
    return encoded, None


def _matches_grant(url: str, grants: tuple[tuple[str, str], ...]) -> tuple[str, str] | None:
    request = urlsplit(url)
    request_host = (request.hostname or "").casefold()
    request_path = request.path or "/"
    for skill_name, raw_prefix in grants:
        prefix = canonical_https_prefix(raw_prefix)
        if prefix is None:
            continue
        parsed = urlsplit(prefix)
        prefix_path = parsed.path or "/"
        path_matches = (
            request_path.startswith(prefix_path)
            if prefix_path.endswith("/")
            else request_path == prefix_path
        )
        if request_host == (parsed.hostname or "").casefold() and path_matches:
            return skill_name, prefix
    return None


def _matched_grant_receipt(
    skill_name: str,
    canonical_prefix: str,
) -> dict[str, str]:
    """Return a safe exact-grant identity for orchestration audit events."""

    return {
        "matched_skill": str(skill_name),
        "matched_prefix_sha256": hashlib.sha256(
            canonical_prefix.encode("utf-8")
        ).hexdigest(),
    }


def _retrieval_receipt(
    *,
    method: str,
    request_url: str,
    request_body: dict[str, Any] | None,
    response_body: str,
    pagination_scan_body: str | None,
    body_truncated: bool,
    body_spilled_complete: bool = False,
    wire_body_complete: bool,
    response_bytes_read: int,
    response_chars_read: int,
    max_chars: int,
    timeout: int,
    request_number: int,
    request_elapsed_ms: int,
    grants: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    """Build non-authoritative pagination metadata for one HTTP receipt.

    A server-provided next URL is labelled only for model guidance. Every
    subsequent dispatch still crosses the normal canonical URL and exact-grant
    checks; this metadata can never mint network authority.
    """

    receipt = build_http_retrieval_receipt(
        method=method,
        request_url=request_url,
        request_body=request_body,
        response_body=response_body,
        pagination_scan_body=pagination_scan_body,
        body_truncated=body_truncated,
        body_spilled_complete=body_spilled_complete,
        wire_body_complete=wire_body_complete,
        response_bytes_read=response_bytes_read,
        response_byte_limit=MAX_RESPONSE_BYTES,
        response_chars_read=response_chars_read,
        response_chars_returned=len(response_body),
        response_char_limit=max_chars,
        response_char_hard_limit=MAX_CHARS,
        request_timeout=timeout,
        request_number=request_number,
        request_run_hop_limit=_configured_max_requests_per_run(),
        request_elapsed_ms=request_elapsed_ms,
    )
    pagination = receipt.get("pagination")
    if isinstance(pagination, dict):
        for hint in pagination.get("next_hints") or []:
            if not isinstance(hint, dict) or hint.get("kind") != "url":
                continue
            hint["authorized_by_current_grants"] = bool(
                _matches_grant(str(hint.get("value") or ""), grants)
            )
    action = receipt.get("continuation_action")
    if isinstance(action, dict):
        args = action.get("args")
        action_url = str(args.get("url") or "") if isinstance(args, dict) else ""
        if action_url and _matches_grant(action_url, grants) is None:
            receipt["continuation_action"] = {
                "version": action.get("version"),
                "kind": "degrade",
                "tool_name": action.get("tool_name"),
                "reason": "pagination_continuation_outside_current_grants",
            }
    return receipt


async def _public_addresses(hostname: str) -> tuple[tuple[str, int], ...]:
    if not _DNS_SLOTS.acquire(blocking=False):
        raise OSError("bounded Skill HTTP DNS capacity is busy")

    def resolve_records():
        return socket.getaddrinfo(
            hostname,
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )

    loop = asyncio.get_running_loop()
    try:
        resolver_future = _DNS_EXECUTOR.submit(resolve_records)
    except BaseException:
        _DNS_SLOTS.release()
        raise
    # A concurrent-future callback runs only when the underlying lookup has
    # actually completed or was canceled before it began. It therefore does
    # not release capacity early when ``asyncio.wait_for`` cancels its wrapper
    # while a resolver thread remains blocked.
    resolver_future.add_done_callback(lambda _future: _DNS_SLOTS.release())
    records = await asyncio.wrap_future(resolver_future, loop=loop)
    addresses: list[tuple[str, int]] = []
    for family, _socktype, _proto, _canonname, sockaddr in records:
        address = str(sockaddr[0])
        parsed = ipaddress.ip_address(address)
        transition_address = bool(
            isinstance(parsed, ipaddress.IPv6Address)
            and (
                parsed.ipv4_mapped is not None
                or parsed.sixtofour is not None
                or parsed.teredo is not None
                or parsed in ipaddress.ip_network("64:ff9b::/96")
                or parsed in ipaddress.ip_network("64:ff9b:1::/48")
            )
        )
        if not parsed.is_global or transition_address:
            raise ValueError("authorized hostname resolved to a non-public address")
        item = (address, family)
        if item not in addresses:
            addresses.append(item)
    if not addresses:
        raise ValueError("authorized hostname did not resolve to a public address")
    return tuple(sorted(addresses, key=lambda item: (item[1], item[0])))


def _increment_lru_count(
    values: OrderedDict[str, int],
    identity: str,
    count: int,
) -> None:
    values.pop(identity, None)
    values[identity] = count
    while len(values) > MAX_TRACKED_RUNS:
        values.popitem(last=False)


def _active_limit_error(
    code: str,
    message: str,
    *,
    request_number: int,
    root_request_number: int,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "request_number": request_number,
        "root_request_number": root_request_number,
    }


def _quota_identities(context: ToolContext) -> tuple[str, str, str]:
    """Return tenant-scoped run, root-run, and user quota identities."""

    user_identity = str(context.user_id or "default")
    run_token = str(context.run_id or context.session_id or "default")
    root_token = str(
        context.root_run_id
        or context.run_id
        or context.session_id
        or "default"
    )
    return (
        f"{user_identity}\x1f{run_token}",
        f"{user_identity}\x1f{root_token}",
        user_identity,
    )


def _acquire_request_slot(
    context: ToolContext,
    hostname: str,
    *,
    now: float,
) -> tuple[_RequestLease | None, dict[str, Any] | None]:
    """Atomically consume one hop quota and reserve bounded concurrency."""

    global _active_request_total

    # Runtime ids are expected to be globally unique, but tenant-prefixing
    # makes quota isolation explicit even for legacy/custom callers.
    run_identity, root_identity, user_identity = _quota_identities(context)
    host_identity = hostname.rstrip(".").casefold()

    with _request_count_lock:
        run_number = _request_counts.get(run_identity, 0) + 1
        root_number = _root_request_counts.get(root_identity, 0) + 1
        run_request_limit = _configured_max_requests_per_run()
        if run_number > run_request_limit:
            return None, _active_limit_error(
                "skill_http_request_limit",
                f"This delegated run exceeded the {run_request_limit}-hop Skill HTTP limit.",
                request_number=run_number,
                root_request_number=root_number,
            )
        if root_number > MAX_REQUESTS_PER_ROOT_RUN:
            return None, _active_limit_error(
                "skill_http_root_request_limit",
                f"This root workflow exceeded the {MAX_REQUESTS_PER_ROOT_RUN}-hop Skill HTTP limit.",
                request_number=run_number,
                root_request_number=root_number,
            )

        window = _user_request_windows.pop(user_identity, deque())
        cutoff = now - USER_REQUEST_WINDOW_SECONDS
        while window and window[0] <= cutoff:
            window.popleft()
        _user_request_windows[user_identity] = window
        while len(_user_request_windows) > MAX_TRACKED_USERS:
            _user_request_windows.popitem(last=False)
        if len(window) >= MAX_REQUESTS_PER_USER_WINDOW:
            return None, _active_limit_error(
                "skill_http_user_rate_limit",
                "This user exceeded the bounded Skill HTTP request window.",
                request_number=run_number,
                root_request_number=root_number,
            )

        concurrency_error: tuple[str, str] | None = None
        if _active_request_total >= MAX_CONCURRENT_REQUESTS:
            concurrency_error = (
                "skill_http_global_concurrency_limit",
                "The process-wide Skill HTTP concurrency limit is busy.",
            )
        elif _active_by_host.get(host_identity, 0) >= MAX_CONCURRENT_PER_HOST:
            concurrency_error = (
                "skill_http_host_concurrency_limit",
                "The target host's Skill HTTP concurrency limit is busy.",
            )
        elif _active_by_root.get(root_identity, 0) >= MAX_CONCURRENT_PER_ROOT_RUN:
            concurrency_error = (
                "skill_http_root_concurrency_limit",
                "The root workflow's Skill HTTP concurrency limit is busy.",
            )
        elif _active_by_user.get(user_identity, 0) >= MAX_CONCURRENT_PER_USER:
            concurrency_error = (
                "skill_http_user_concurrency_limit",
                "The user's Skill HTTP concurrency limit is busy.",
            )
        if concurrency_error is not None:
            return None, _active_limit_error(
                concurrency_error[0],
                concurrency_error[1],
                request_number=run_number,
                root_request_number=root_number,
            )

        _increment_lru_count(_request_counts, run_identity, run_number)
        _increment_lru_count(_root_request_counts, root_identity, root_number)
        window.append(now)
        _active_request_total += 1
        _active_by_host[host_identity] = _active_by_host.get(host_identity, 0) + 1
        _active_by_root[root_identity] = _active_by_root.get(root_identity, 0) + 1
        _active_by_user[user_identity] = _active_by_user.get(user_identity, 0) + 1
        return _RequestLease(
            host=host_identity,
            root_identity=root_identity,
            user_identity=user_identity,
            request_number=run_number,
            root_request_number=root_number,
        ), None


def _release_request_slot(lease: _RequestLease) -> None:
    global _active_request_total

    with _request_count_lock:
        _active_request_total = max(0, _active_request_total - 1)
        for values, identity in (
            (_active_by_host, lease.host),
            (_active_by_root, lease.root_identity),
            (_active_by_user, lease.user_identity),
        ):
            remaining = values.get(identity, 0) - 1
            if remaining > 0:
                values[identity] = remaining
            else:
                values.pop(identity, None)


async def skill_http_get(
    url: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout: int = DEFAULT_TIMEOUT,
    candidate_id: str | None = None,
    context: ToolContext | None = None,
) -> str:
    """GET one exact Skill-declared HTTPS endpoint through a pinned resolver.

    ``candidate_id`` is a model-visible routing handle only.  The AgentLoop
    validates it against the immutable active Knowledge Gate frontier and
    narrows ``context`` to that candidate's already-authorized grant before
    this handler is entered.  It can never create or widen HTTP authority.
    Ordinary non-gated calls may omit it.
    """

    del candidate_id

    try:
        require_execution_authority(
            context,
            boundary="skill_http_get.entry",
        )
    except ExecutionAuthorityRevoked:
        return _error(
            "execution_authority_revoked",
            "Delegated execution authority was revoked; no request was sent.",
            request_sent=False,
        )
    if context is None or not context.allowed_skill_http_prefixes:
        return _error(
            "missing_skill_http_grant",
            "Skill HTTP GET requires a runtime-owned literal endpoint grant.",
            request_sent=False,
        )
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= MAX_CHARS:
        return _error("invalid_max_chars", f"max_chars must be between 1 and {MAX_CHARS}.", request_sent=False)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
        return _error("invalid_timeout", f"timeout must be between 1 and {MAX_TIMEOUT} seconds.", request_sent=False)
    current, url_error = _canonical_request_url(url)
    if current is None:
        return _error("invalid_url", str(url_error), request_sent=False)
    matched = _matches_grant(current, context.allowed_skill_http_prefixes)
    if matched is None:
        return _error(
            "skill_http_boundary_violation",
            "URL is outside the literal HTTPS prefixes compiled from this task's Skill closure.",
            request_sent=False,
        )

    matched_skill, matched_prefix = matched
    matched_receipt = _matched_grant_receipt(
        matched_skill,
        matched_prefix,
    )
    redirects = 0
    request_sent = False
    request_number = 0
    root_request_number = 0
    loop = asyncio.get_running_loop()
    request_started_at = loop.time()
    deadline = loop.time() + timeout
    try:
        while True:
            parsed = urlsplit(current)
            hostname = str(parsed.hostname or "")
            lease, admission_error = _acquire_request_slot(
                context,
                hostname,
                now=loop.time(),
            )
            if lease is None:
                assert admission_error is not None
                return _error(
                    str(admission_error["code"]),
                    str(admission_error["message"]),
                    request_sent=request_sent,
                    request_number=admission_error["request_number"],
                    root_request_number=admission_error[
                        "root_request_number"
                    ],
                    redirects_followed=redirects,
                    **matched_receipt,
                )
            request_number = lease.request_number
            root_request_number = lease.root_request_number
            try:
                remaining_seconds = deadline - loop.time()
                if remaining_seconds <= 0:
                    raise asyncio.TimeoutError
                addresses = await asyncio.wait_for(
                    _public_addresses(hostname),
                    timeout=remaining_seconds,
                )
                resolver = _PinnedResolver(hostname, addresses)
                connector = aiohttp.TCPConnector(
                    resolver=resolver,
                    use_dns_cache=True,
                    ttl_dns_cache=None,
                    limit=1,
                    ssl=True,
                )
                remaining_seconds = deadline - loop.time()
                if remaining_seconds <= 0:
                    await connector.close()
                    raise asyncio.TimeoutError
                client_timeout = aiohttp.ClientTimeout(
                    total=remaining_seconds,
                )
                # ``asyncio.timeout`` keeps the total user-supplied deadline
                # authoritative even for tests/custom connectors that do not
                # honor aiohttp's timeout object themselves.
                async with asyncio.timeout(remaining_seconds):
                    async with aiohttp.ClientSession(
                        connector=connector,
                        timeout=client_timeout,
                        trust_env=False,
                        headers={
                            "User-Agent": "ChatDS-SkillHTTP/1.0",
                            "Accept": (
                                "application/json, application/xml, text/xml, "
                                "text/plain, text/html;q=0.5"
                            ),
                        },
                    ) as session:
                        # This is an auditable dispatch attempt even if TLS or
                        # the peer fails before response headers arrive.
                        require_execution_authority(
                            context,
                            boundary="skill_http_get.request_submit",
                        )
                        request_sent = True
                        async with session.get(
                            current,
                            allow_redirects=False,
                        ) as response:
                            if response.status in {301, 302, 303, 307, 308}:
                                location = response.headers.get("Location")
                                if not location or redirects >= MAX_REDIRECTS:
                                    return _error(
                                        "unsafe_redirect",
                                        "Skill endpoint returned a missing or excessive redirect.",
                                        request_sent=True,
                                        request_number=request_number,
                                        root_request_number=root_request_number,
                                        http_status=response.status,
                                        redirects_followed=redirects,
                                        **matched_receipt,
                                    )
                                redirected, redirect_error = _canonical_request_url(
                                    urljoin(current, location)
                                )
                                if redirected is None or _matches_grant(
                                    redirected,
                                    context.allowed_skill_http_prefixes,
                                ) != matched:
                                    return _error(
                                        "skill_http_redirect_boundary_violation",
                                        str(
                                            redirect_error
                                            or "redirect escaped the exact Skill HTTPS grant"
                                        ),
                                        request_sent=True,
                                        request_number=request_number,
                                        root_request_number=root_request_number,
                                        http_status=response.status,
                                        redirects_followed=redirects,
                                        **matched_receipt,
                                    )
                                current = redirected
                                redirects += 1
                                continue

                            content_type = (
                                response.headers.get("Content-Type", "")
                                .split(";", 1)[0]
                                .strip()
                                .casefold()
                            )
                            if content_type and not (
                                content_type.startswith("text/")
                                or content_type in {
                                    "application/json",
                                    "application/xml",
                                    "application/xhtml+xml",
                                    "application/problem+json",
                                }
                                or content_type.endswith("+json")
                                or content_type.endswith("+xml")
                            ):
                                return _error(
                                    "unsupported_content_type",
                                    "Skill HTTP GET accepts only text, JSON, or XML "
                                    f"responses (received {content_type}).",
                                    request_sent=True,
                                    request_number=request_number,
                                    root_request_number=root_request_number,
                                    http_status=response.status,
                                    redirects_followed=redirects,
                                    **matched_receipt,
                                )
                            raw, truncated_bytes = (
                                await _read_bounded_response_bytes(response)
                            )
                            charset = response.charset or "utf-8"
                            try:
                                full_body = raw.decode(
                                    charset, errors="replace"
                                )
                            except LookupError:
                                full_body = raw.decode(
                                    "utf-8", errors="replace"
                                )
                            wire_body_complete = not truncated_bytes
                            body_truncated = (
                                not wire_body_complete
                                or len(full_body) > max_chars
                            )
                            body = full_body[:max_chars]
                            body_result_handle = None
                            if (
                                wire_body_complete
                                and len(full_body) > max_chars
                            ):
                                body_result_handle = persist_tool_result_spill(
                                    full_body,
                                    "skill_http_get_body",
                                    user_id=context.user_id,
                                    session_id=context.session_id,
                                )
                            body_spilled_complete = bool(body_result_handle)
                            payload = {
                                "status": (
                                    "success"
                                    if 200 <= response.status < 300
                                    else "error"
                                ),
                                "request_sent": True,
                                "request_number": request_number,
                                "root_request_number": root_request_number,
                                "matched_skill": matched_skill,
                                "matched_prefix": matched_prefix,
                                **matched_receipt,
                                "url": current,
                                "http_status": response.status,
                                "content_type": content_type,
                                "body_result_handle": body_result_handle,
                                "body": body,
                                "body_chars": len(body),
                                "body_truncated": body_truncated,
                                "body_spilled_complete": (
                                    body_spilled_complete
                                ),
                                "body_sha256": hashlib.sha256(raw).hexdigest(),
                                "redirects_followed": redirects,
                                "transport_retry_count": (
                                    _GET_TRANSPORT_RETRY_DEPTH.get()
                                ),
                                "retrieval": _retrieval_receipt(
                                    method="GET",
                                    request_url=current,
                                    request_body=None,
                                    response_body=body,
                                    pagination_scan_body=(
                                        full_body
                                        if wire_body_complete else None
                                    ),
                                    body_truncated=body_truncated,
                                    body_spilled_complete=(
                                        body_spilled_complete
                                    ),
                                    wire_body_complete=wire_body_complete,
                                    response_bytes_read=len(raw),
                                    response_chars_read=len(full_body),
                                    max_chars=max_chars,
                                    timeout=timeout,
                                    request_number=request_number,
                                    request_elapsed_ms=int(
                                        max(
                                            0.0,
                                            loop.time() - request_started_at,
                                        ) * 1000
                                    ),
                                    grants=(
                                        context.allowed_skill_http_prefixes
                                    ),
                                ),
                            }
                            if not 200 <= response.status < 300:
                                payload["error_code"] = "http_status_error"
                                payload["error"] = (
                                    "Skill endpoint returned HTTP "
                                    f"{response.status}."
                                )
                            return json.dumps(payload, ensure_ascii=False)
            finally:
                _release_request_slot(lease)
    except ExecutionAuthorityRevoked:
        return _error(
            "execution_authority_revoked",
            "Delegated execution authority was revoked; no further request "
            "was sent.",
            request_sent=request_sent,
            request_number=request_number,
            root_request_number=root_request_number,
            redirects_followed=redirects,
            **matched_receipt,
        )
    except asyncio.TimeoutError:
        return _error(
            "skill_http_timeout",
            f"Skill HTTP GET timed out after the total {timeout}s deadline.",
            request_sent=request_sent,
            request_number=request_number,
            root_request_number=root_request_number,
            redirects_followed=redirects,
            **matched_receipt,
        )
    except (aiohttp.ClientError, OSError) as exc:
        # GET is the only idempotent bridge. Retry one pre-submit transport/DNS
        # failure inside the caller's original total deadline; the recursive
        # attempt reacquires admission so both attempts remain quota-visible.
        # Once request submission was entered, preserve its exact side-effect
        # attribution instead of hiding it behind a second attempt. POST and
        # policy/status failures are never replayed.
        remaining_seconds = deadline - loop.time()
        if (
            _GET_TRANSPORT_RETRY_DEPTH.get() == 0
            and not request_sent
            and remaining_seconds >= 1.0
        ):
            token = _GET_TRANSPORT_RETRY_DEPTH.set(1)
            try:
                retry_timeout = max(
                    1,
                    min(MAX_TIMEOUT, int(remaining_seconds)),
                )
                return await skill_http_get(
                    current,
                    max_chars=max_chars,
                    timeout=retry_timeout,
                    candidate_id=None,
                    context=context,
                )
            finally:
                _GET_TRANSPORT_RETRY_DEPTH.reset(token)
        return _error(
            "skill_http_transport_error",
            f"Skill HTTP GET failed safely ({type(exc).__name__}: {exc}).",
            request_sent=request_sent,
            request_number=request_number,
            root_request_number=root_request_number,
            redirects_followed=redirects,
            transport_retry_count=_GET_TRANSPORT_RETRY_DEPTH.get(),
            **matched_receipt,
        )
    except ValueError as exc:
        return _error(
            "skill_http_transport_error",
            f"Skill HTTP GET failed safely ({type(exc).__name__}: {exc}).",
            request_sent=request_sent,
            request_number=request_number,
            root_request_number=root_request_number,
            redirects_followed=redirects,
            transport_retry_count=_GET_TRANSPORT_RETRY_DEPTH.get(),
            **matched_receipt,
        )


async def skill_http_post_json(
    url: str,
    body: dict[str, Any],
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout: int = DEFAULT_TIMEOUT,
    candidate_id: str | None = None,
    context: ToolContext | None = None,
) -> str:
    """POST one bounded JSON object to an exact Skill-declared HTTPS grant.

    This intentionally is not a general HTTP client: the model cannot supply
    headers, cookies, credentials, a media type, or redirect policy.  POST
    redirects fail closed because 301/302/303 can rewrite the method and even
    307/308 would repeat a potentially state-changing request.
    """

    # See ``skill_http_get``: this identifier is validated and consumed by
    # orchestration.  The bridge relies only on runtime-owned narrowed grants.
    del candidate_id

    try:
        require_execution_authority(
            context,
            boundary="skill_http_post_json.entry",
        )
    except ExecutionAuthorityRevoked:
        return _error(
            "execution_authority_revoked",
            "Delegated execution authority was revoked; no request was sent.",
            request_sent=False,
        )
    if context is None or not context.allowed_skill_http_post_prefixes:
        return _error(
            "missing_skill_http_grant",
            "Skill HTTP JSON POST requires an explicit runtime-owned POST/GraphQL endpoint grant.",
            request_sent=False,
        )
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not 1 <= max_chars <= MAX_CHARS
    ):
        return _error(
            "invalid_max_chars",
            f"max_chars must be between 1 and {MAX_CHARS}.",
            request_sent=False,
        )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= MAX_TIMEOUT
    ):
        return _error(
            "invalid_timeout",
            f"timeout must be between 1 and {MAX_TIMEOUT} seconds.",
            request_sent=False,
        )
    encoded_body, body_error = _json_body_bytes(body)
    if encoded_body is None:
        return _error(
            "invalid_json_body",
            str(body_error),
            request_sent=False,
        )
    current, url_error = _canonical_request_url(url)
    if current is None:
        return _error("invalid_url", str(url_error), request_sent=False)
    matched = _matches_grant(
        current, context.allowed_skill_http_post_prefixes
    )
    if matched is None:
        return _error(
            "skill_http_boundary_violation",
            "URL is outside the literal HTTPS prefixes compiled from this task's Skill closure.",
            request_sent=False,
        )

    matched_skill, matched_prefix = matched
    matched_receipt = _matched_grant_receipt(
        matched_skill,
        matched_prefix,
    )
    request_sent = False
    request_number = 0
    root_request_number = 0
    parsed = urlsplit(current)
    hostname = str(parsed.hostname or "")
    loop = asyncio.get_running_loop()
    request_started_at = loop.time()
    deadline = loop.time() + timeout
    lease, admission_error = _acquire_request_slot(
        context,
        hostname,
        now=loop.time(),
    )
    if lease is None:
        assert admission_error is not None
        return _error(
            str(admission_error["code"]),
            str(admission_error["message"]),
            request_sent=False,
            request_number=admission_error["request_number"],
            root_request_number=admission_error["root_request_number"],
            redirects_followed=0,
            **matched_receipt,
        )
    request_number = lease.request_number
    root_request_number = lease.root_request_number
    try:
        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            raise asyncio.TimeoutError
        addresses = await asyncio.wait_for(
            _public_addresses(hostname),
            timeout=remaining_seconds,
        )
        resolver = _PinnedResolver(hostname, addresses)
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=True,
            ttl_dns_cache=None,
            limit=1,
            ssl=True,
        )
        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            await connector.close()
            raise asyncio.TimeoutError
        client_timeout = aiohttp.ClientTimeout(total=remaining_seconds)
        async with asyncio.timeout(remaining_seconds):
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=client_timeout,
                trust_env=False,
                headers={
                    "User-Agent": "ChatDS-SkillHTTP/1.0",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            ) as session:
                require_execution_authority(
                    context,
                    boundary="skill_http_post_json.request_submit",
                )
                request_sent = True
                async with session.post(
                    current,
                    data=encoded_body,
                    allow_redirects=False,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        return _error(
                            "unsafe_post_redirect",
                            "Skill HTTP JSON POST never follows redirects.",
                            request_sent=True,
                            request_number=request_number,
                            root_request_number=root_request_number,
                            http_status=response.status,
                            redirects_followed=0,
                            **matched_receipt,
                        )

                    content_type = (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .casefold()
                    )
                    if not (
                        content_type == "application/json"
                        or content_type.endswith("+json")
                    ):
                        return _error(
                            "unsupported_content_type",
                            "Skill HTTP JSON POST accepts only JSON responses "
                            f"(received {content_type or 'no content type'}).",
                            request_sent=True,
                            request_number=request_number,
                            root_request_number=root_request_number,
                            http_status=response.status,
                            redirects_followed=0,
                            **matched_receipt,
                        )

                    raw, truncated_bytes = (
                        await _read_bounded_response_bytes(response)
                    )
                    charset = response.charset or "utf-8"
                    try:
                        full_response_body = raw.decode(
                            charset, errors="replace"
                        )
                    except LookupError:
                        full_response_body = raw.decode(
                            "utf-8", errors="replace"
                        )
                    wire_body_complete = not truncated_bytes
                    body_truncated = (
                        not wire_body_complete
                        or len(full_response_body) > max_chars
                    )
                    response_body = full_response_body[:max_chars]
                    body_result_handle = None
                    if (
                        wire_body_complete
                        and len(full_response_body) > max_chars
                    ):
                        body_result_handle = persist_tool_result_spill(
                            full_response_body,
                            "skill_http_post_json_body",
                            user_id=context.user_id,
                            session_id=context.session_id,
                        )
                    body_spilled_complete = bool(body_result_handle)
                    payload = {
                        "status": (
                            "success"
                            if 200 <= response.status < 300
                            else "error"
                        ),
                        "request_sent": True,
                        "request_method": "POST",
                        "request_number": request_number,
                        "root_request_number": root_request_number,
                        "request_body_bytes": len(encoded_body),
                        "request_body_sha256": hashlib.sha256(
                            encoded_body
                        ).hexdigest(),
                        "matched_skill": matched_skill,
                        "matched_prefix": matched_prefix,
                        **matched_receipt,
                        "url": current,
                        "http_status": response.status,
                        "content_type": content_type,
                        "body_result_handle": body_result_handle,
                        "body": response_body,
                        "body_chars": len(response_body),
                        "body_truncated": body_truncated,
                        "body_spilled_complete": body_spilled_complete,
                        "body_sha256": hashlib.sha256(raw).hexdigest(),
                        "redirects_followed": 0,
                        "retrieval": _retrieval_receipt(
                            method="POST",
                            request_url=current,
                            request_body=body,
                            response_body=response_body,
                            pagination_scan_body=(
                                full_response_body
                                if wire_body_complete else None
                            ),
                            body_truncated=body_truncated,
                            body_spilled_complete=body_spilled_complete,
                            wire_body_complete=wire_body_complete,
                            response_bytes_read=len(raw),
                            response_chars_read=len(full_response_body),
                            max_chars=max_chars,
                            timeout=timeout,
                            request_number=request_number,
                            request_elapsed_ms=int(
                                max(
                                    0.0,
                                    loop.time() - request_started_at,
                                ) * 1000
                            ),
                            grants=(
                                context.allowed_skill_http_post_prefixes
                            ),
                        ),
                    }
                    if not 200 <= response.status < 300:
                        payload["error_code"] = "http_status_error"
                        payload["error"] = (
                            "Skill endpoint returned HTTP "
                            f"{response.status}."
                        )
                    return json.dumps(payload, ensure_ascii=False)
    except ExecutionAuthorityRevoked:
        return _error(
            "execution_authority_revoked",
            "Delegated execution authority was revoked; no request was sent.",
            request_sent=request_sent,
            request_number=request_number,
            root_request_number=root_request_number,
            redirects_followed=0,
            **matched_receipt,
        )
    except asyncio.TimeoutError:
        return _error(
            "skill_http_timeout",
            f"Skill HTTP JSON POST timed out after the total {timeout}s deadline.",
            request_sent=request_sent,
            request_number=request_number,
            root_request_number=root_request_number,
            redirects_followed=0,
            **matched_receipt,
        )
    except (aiohttp.ClientError, OSError, ValueError) as exc:
        return _error(
            "skill_http_transport_error",
            "Skill HTTP JSON POST failed safely "
            f"({type(exc).__name__}: {exc}).",
            request_sent=request_sent,
            request_number=request_number,
            root_request_number=root_request_number,
            redirects_followed=0,
            **matched_receipt,
        )
    finally:
        _release_request_slot(lease)


RUN_SKILL_HTTP_GET_SCHEMA = {
    "name": "skill_http_get",
    "description": (
        "Perform a bounded credential-free HTTPS GET only against literal URL prefixes compiled "
        "from the exact current-task Skill package. Redirects must remain in the same grant; DNS "
        "is pinned to prevalidated public addresses. Use this for instruction-only REST/API Skills."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full HTTPS URL under one exact Skill-declared prefix; no credentials or fragments.",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_CHARS,
                "default": DEFAULT_MAX_CHARS,
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TIMEOUT,
                "default": DEFAULT_TIMEOUT,
            },
            "candidate_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                "description": (
                    "Exact runtime-listed Knowledge Gate candidate handle. "
                    "Required only when the current pending frontier schema "
                    "marks it required; it never grants network authority."
                ),
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}


RUN_SKILL_HTTP_POST_JSON_SCHEMA = {
    "name": "skill_http_post_json",
    "description": (
        "POST one bounded credential-free JSON object only to an exact HTTPS URL prefix "
        "compiled from the current Skill package. The runtime fixes Content-Type to "
        "application/json, pins DNS, never follows redirects, and accepts no custom "
        "headers, cookies, or credentials. Use for declared JSON/GraphQL-style APIs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "Full HTTPS URL under one exact Skill-declared prefix; "
                    "no credentials or fragments."
                ),
            },
            "body": {
                "type": "object",
                "description": (
                    "JSON request object. Runtime limits depth, nodes, encoded "
                    "bytes, non-finite numbers, and credential-bearing properties."
                ),
                "maxProperties": MAX_JSON_BODY_NODES,
                "additionalProperties": True,
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_CHARS,
                "default": DEFAULT_MAX_CHARS,
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TIMEOUT,
                "default": DEFAULT_TIMEOUT,
            },
            "candidate_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                "description": (
                    "Exact runtime-listed Knowledge Gate candidate handle. "
                    "Required only when the current pending frontier schema "
                    "marks it required; it never grants network authority."
                ),
            },
        },
        "required": ["url", "body"],
        "additionalProperties": False,
    },
}
