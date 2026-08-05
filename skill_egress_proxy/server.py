"""Fail-closed HTTP policy proxy for isolated session-sandbox workers.

The worker container has ``network_mode:none``. This separately networked
service is its only egress path, so changing browser flags or ignoring proxy
environment variables cannot create a direct route. Every request must match
an authenticated method-and-URL-prefix rule before its destination is resolved,
classified, and pinned.

This is deliberately not a general forward proxy.  It supports the two forms
used by browsers (CONNECT for HTTPS and absolute-form HTTP), limits ports,
blocks non-public addresses by default, and never logs URL paths. Every
connection carries a fresh authenticated method-and-URL-prefix policy; its
derived origin projection is used only for DNS/connect authorization. The
standalone handler has no ambient public-web authority and denies by default.
"""

from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
import hashlib
import hmac
import ipaddress
import json
import os
import re
import selectors
import secrets
import shutil
import socket
import socketserver
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable
from urllib.parse import urlsplit, urlunsplit


LISTEN_HOST: Final[str] = os.environ.get("SKILL_EGRESS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT: Final[int] = int(os.environ.get("SKILL_EGRESS_LISTEN_PORT", "8080"))
LISTEN_SOCKET: Final[str] = os.environ.get("SKILL_EGRESS_SOCKET_PATH", "").strip()
EGRESS_POLICY_RUNTIME_VERSION: Final[str] = "signed-exact-query-v1"
CONNECT_TIMEOUT_SECONDS: Final[float] = float(
    os.environ.get("SKILL_EGRESS_CONNECT_TIMEOUT_SECONDS", "10")
)
IDLE_TIMEOUT_SECONDS: Final[float] = float(
    os.environ.get("SKILL_EGRESS_IDLE_TIMEOUT_SECONDS", "30")
)
MAX_TUNNEL_SECONDS: Final[float] = float(
    os.environ.get("SKILL_EGRESS_MAX_TUNNEL_SECONDS", "14400")
)
MAX_HEADER_BYTES: Final[int] = 64 * 1024
MAX_BUFFER_BYTES: Final[int] = 256 * 1024
COPY_CHUNK_BYTES: Final[int] = 64 * 1024
MAX_HTTP_REQUEST_BODY_BYTES: Final[int] = 8 * 1024 * 1024
MAX_HTTP_REQUEST_TARGET_BYTES: Final[int] = 8 * 1024
MAX_HTTP_REQUEST_HEADER_FIELDS: Final[int] = 128
MAX_HTTP_REQUEST_HEADER_NAME_BYTES: Final[int] = 256
MAX_HTTP_REQUEST_HEADER_VALUE_BYTES: Final[int] = 16 * 1024
MAX_HTTP_QUERY_FIELDS: Final[int] = 128
MAX_HTTP_QUERY_KEY_BYTES: Final[int] = 512
MAX_HTTP_QUERY_VALUE_BYTES: Final[int] = 4 * 1024
TLS_HANDSHAKE_TIMEOUT_SECONDS: Final[float] = 15.0
POLICY_PREFACE_ABSOLUTE_READ_TIMEOUT_SECONDS: Final[float] = 5.0
HTTP_HEADER_ABSOLUTE_READ_TIMEOUT_SECONDS: Final[float] = 15.0
HTTP_READ_IDLE_TIMEOUT_SECONDS: Final[float] = 5.0
HTTP_BODY_ABSOLUTE_READ_TIMEOUT_SECONDS: Final[float] = 60.0
HTTP_BODY_IDLE_TIMEOUT_SECONDS: Final[float] = 10.0
UPSTREAM_REQUEST_WRITE_TIMEOUT_SECONDS: Final[float] = 30.0
ERROR_INPUT_DRAIN_ABSOLUTE_TIMEOUT_SECONDS: Final[float] = 1.0
MAX_ERROR_INPUT_DRAIN_BYTES: Final[int] = MAX_HEADER_BYTES
MAX_INTERCEPTION_CERTIFICATES: Final[int] = 512
INTERCEPTION_CERTIFICATE_REFRESH_SECONDS: Final[float] = 12 * 60 * 60
MAX_CONCURRENT_CONNECTIONS: Final[int] = int(
    os.environ.get("SKILL_EGRESS_MAX_CONCURRENT_CONNECTIONS", "64")
)
MAX_GLOBAL_CONNECTION_LIMIT: Final[int] = 64
MAX_ORIGIN_ALLOWLIST_ENTRIES: Final[int] = 128
MAX_EXACT_EGRESS_RULES: Final[int] = 256
EGRESS_METHOD_ORDER: Final[tuple[str, ...]] = (
    "GET",
    "HEAD",
    "OPTIONS",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
)
EGRESS_METHODS: Final[frozenset[str]] = frozenset(
    EGRESS_METHOD_ORDER
)
POLICY_PREFACE_PREFIX: Final[bytes] = b"CHATDS-EGRESS-POLICY-V1 "
MAX_POLICY_PREFACE_BYTES: Final[int] = 64 * 1024
MAX_POLICY_TTL_SECONDS: Final[int] = 60
POLICY_CLOCK_SKEW_SECONDS: Final[int] = 5
POLICY_KEY_DERIVATION_LABEL: Final[bytes] = (
    b"chatds-skill-egress-policy-hmac-v1"
)
_HTTP_HEADER_NAME_RE: Final[re.Pattern[bytes]] = re.compile(
    rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$"
)
_HTTP_METHOD_RE: Final[re.Pattern[bytes]] = re.compile(
    rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$"
)
_INVALID_EGRESS_PERCENT_ESCAPE: Final[re.Pattern[str]] = re.compile(
    r"%(?![0-9A-F]{2})"
)
_INVALID_EGRESS_ENCODED_PATH: Final[re.Pattern[str]] = re.compile(
    r"%(?:2e|2f|5c|25|23|3f|0[0-9a-f]|1[0-9a-f]|7f)",
    re.IGNORECASE,
)
PUBLIC_TRUST_DIRECTORY: Final[Path] = Path(
    os.environ.get(
        "SKILL_EGRESS_PUBLIC_TRUST_DIRECTORY",
        "/run/chatds-skill-egress",
    )
)
PUBLIC_CA_CERTIFICATE_PATH: Final[Path] = (
    PUBLIC_TRUST_DIRECTORY / "ca.pem"
)
PUBLIC_LEAF_SPKI_PATH: Final[Path] = (
    PUBLIC_TRUST_DIRECTORY / "leaf.spki"
)
PUBLIC_TRUST_GENERATION_PATH: Final[Path] = (
    PUBLIC_TRUST_DIRECTORY / "generation.json"
)
PRIVATE_TRUST_DIRECTORY: Final[Path] = Path(
    os.environ.get(
        "SKILL_EGRESS_PRIVATE_TRUST_DIRECTORY",
        "/var/lib/chatds-skill-egress-private",
    )
)
OPENSSL_BINARY: Final[str] = "/usr/bin/openssl"
TRUST_GENERATION_MANIFEST_VERSION: Final[int] = 1
MAX_TRUST_GENERATION_MANIFEST_BYTES: Final[int] = 4 * 1024
MAX_CA_CERTIFICATE_BYTES: Final[int] = 64 * 1024
MAX_SPKI_FILE_BYTES: Final[int] = 256
_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ABSOLUTE_MAX_REQUESTS_PER_SCOPE: Final[int] = 65_536
_ABSOLUTE_MAX_OUTBOUND_BYTES_PER_SCOPE: Final[int] = 1024 * 1024 * 1024
_ABSOLUTE_MAX_RESPONSE_WIRE_BYTES_PER_SCOPE: Final[int] = 16 * 1024 * 1024 * 1024
_ABSOLUTE_MAX_POLICY_SCOPE_ENTRIES: Final[int] = 65_536
_ABSOLUTE_MAX_POLICY_SCOPE_TTL_SECONDS: Final[int] = 24 * 60 * 60
_REQUIRE_POLICY_V3_RAW: Final[str] = os.environ.get(
    "SKILL_EGRESS_REQUIRE_POLICY_V3",
    "0",
).strip()
if _REQUIRE_POLICY_V3_RAW not in {"0", "1"}:
    raise RuntimeError("invalid_skill_egress_require_policy_v3")
REQUIRE_POLICY_V3: Final[bool] = _REQUIRE_POLICY_V3_RAW == "1"


def _bounded_positive_env_int(
    name: str,
    default: int,
    absolute_maximum: int,
) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"invalid_{name.lower()}") from exc
    if not 1 <= value <= absolute_maximum:
        raise RuntimeError(f"invalid_{name.lower()}")
    return value


MAX_REQUESTS_PER_SCOPE: Final[int] = _bounded_positive_env_int(
    "SKILL_EGRESS_MAX_REQUESTS",
    8_192,
    _ABSOLUTE_MAX_REQUESTS_PER_SCOPE,
)
MAX_OUTBOUND_BYTES_PER_SCOPE: Final[int] = _bounded_positive_env_int(
    "SKILL_EGRESS_MAX_OUTBOUND_BYTES",
    64 * 1024 * 1024,
    _ABSOLUTE_MAX_OUTBOUND_BYTES_PER_SCOPE,
)
MAX_RESPONSE_WIRE_BYTES_PER_SCOPE: Final[int] = _bounded_positive_env_int(
    "SKILL_EGRESS_MAX_RESPONSE_WIRE_BYTES",
    2 * 1024 * 1024 * 1024,
    _ABSOLUTE_MAX_RESPONSE_WIRE_BYTES_PER_SCOPE,
)
MAX_POLICY_SCOPE_ENTRIES: Final[int] = _bounded_positive_env_int(
    "SKILL_EGRESS_MAX_POLICY_SCOPE_ENTRIES",
    65_536,
    _ABSOLUTE_MAX_POLICY_SCOPE_ENTRIES,
)
POLICY_SCOPE_TTL_SECONDS: Final[int] = _bounded_positive_env_int(
    "SKILL_EGRESS_POLICY_SCOPE_TTL_SECONDS",
    24 * 60 * 60,
    _ABSOLUTE_MAX_POLICY_SCOPE_TTL_SECONDS,
)
MAX_UPSTREAM_TLS_PIN_ENTRIES: Final[int] = 128
MAX_UPSTREAM_TLS_PINS_PER_ORIGIN: Final[int] = 8
_SPKI_SHA256_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9+/]{43}=$"
)
_NAT64_TRANSITION_NETWORKS: Final[tuple[ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


class ProxyPolicyError(ValueError):
    """A stable destination or request-policy rejection."""


class ProxyTransportError(RuntimeError):
    """A post-authorization upstream transport failure."""


class ProxyTransportTimeoutError(ProxyTransportError):
    """A post-authorization upstream transport timeout."""


@dataclass(frozen=True, slots=True)
class Origin:
    """One canonical request-scoped HTTP(S) network origin."""

    scheme: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class ExactEgressRule:
    """One canonical method-and-URL-prefix execution grant."""

    methods: frozenset[str]
    origin: Origin
    path_prefix: str
    query_prefix: str
    query_exact: bool = False


@dataclass(frozen=True, slots=True)
class PolicyBudgetLimits:
    """Authenticated cumulative limits shared by one execution scope."""

    max_requests: int
    max_outbound_bytes: int
    max_response_wire_bytes: int


@dataclass(frozen=True, slots=True)
class SignedEgressPolicy:
    """Authenticated per-connection policy, independent of deployment policy."""

    origins: frozenset[Origin]
    rules: tuple[ExactEgressRule, ...]
    private_origins: frozenset[Origin]
    version: int = 2
    budget_scope_sha256: str | None = None
    call_id_sha256: str | None = None
    limits: PolicyBudgetLimits | None = None


@dataclass(frozen=True, slots=True)
class Destination:
    scheme: str
    host: str
    port: int
    address: str
    family: int
    private_grant: bool


def _is_public_unicast(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Accept only ordinary globally-routable unicast destinations.

    ``ipaddress.is_global`` is intentionally broader than an HTTP egress
    policy on some Python releases (notably for multicast ranges).  Reject
    every special-purpose class explicitly, including an IPv4 address wrapped
    in IPv6, so CONNECT cannot be used as a multicast or local-network probe.
    """

    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Transition mechanisms can hide an IPv4 destination from a policy
        # that validates only the outer IPv6 address.  Browsers do not require
        # these forms for ordinary public HTTP(S), so reject them outright.
        if address.sixtofour is not None or address.teredo is not None:
            return False
        if any(address in network for network in _NAT64_TRANSITION_NETWORKS):
            return False
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
    )


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _normalized_host(value: str) -> str:
    if not isinstance(value, str):
        raise ProxyPolicyError("invalid_destination_host")
    host = value.rstrip(".").casefold()
    if (
        not host
        or "\x00" in host
        or "%" in host
        or any(char in host for char in "*?[]")
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in host)
    ):
        raise ProxyPolicyError("invalid_destination_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return address.compressed
    try:
        normalized = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ProxyPolicyError("invalid_destination_host") from exc
    if (
        len(normalized) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                for char in label
            )
            for label in normalized.split(".")
        )
    ):
        raise ProxyPolicyError("invalid_destination_host")
    return normalized


def _normalized_origin(scheme: str, host: str, port: int) -> Origin:
    if not isinstance(scheme, str) or scheme != scheme.strip():
        raise ProxyPolicyError("invalid_origin_scheme")
    normalized_scheme = scheme.casefold()
    if normalized_scheme not in {"http", "https"}:
        raise ProxyPolicyError("unsupported_destination_scheme")
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise ProxyPolicyError("invalid_destination_port")
    return Origin(
        scheme=normalized_scheme,
        host=_normalized_host(host),
        port=port,
    )


def normalize_origin_allowlist(
    values: Iterable[Origin | tuple[str, str, int]] | None,
) -> frozenset[Origin]:
    """Return a bounded exact-origin allowlist with no wildcard semantics.

    Callers provide a fresh value for one proxy connection/request.  This
    function deliberately accepts no hostname patterns, CIDRs, URL paths, or
    ambient process-wide fallback.  An absent/empty list therefore means
    deny-all.
    """

    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes, bytearray)):
        raise ProxyPolicyError("invalid_origin_allowlist")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ProxyPolicyError("invalid_origin_allowlist") from exc

    normalized: set[Origin] = set()
    for index, value in enumerate(iterator):
        if index >= MAX_ORIGIN_ALLOWLIST_ENTRIES:
            raise ProxyPolicyError("origin_allowlist_too_large")
        if isinstance(value, Origin):
            raw_origin = (value.scheme, value.host, value.port)
        elif isinstance(value, (tuple, list)) and len(value) == 3:
            raw_origin = (value[0], value[1], value[2])
        else:
            raise ProxyPolicyError("invalid_origin_allowlist")
        try:
            origin = _normalized_origin(*raw_origin)
        except TypeError as exc:
            raise ProxyPolicyError("invalid_origin_allowlist") from exc
        normalized.add(origin)
    return frozenset(normalized)


def _origin_tuple(value: str) -> Origin:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed_port = parsed.port
    except ValueError as exc:
        raise ProxyPolicyError(
            "invalid_private_origin_allowlist"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not host
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProxyPolicyError("invalid_private_origin_allowlist")
    port = (
        parsed_port
        if parsed_port is not None
        else (443 if parsed.scheme == "https" else 80)
    )
    return _normalized_origin(parsed.scheme, host, port)


def _origin_text(origin: Origin) -> str:
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    return f"{origin.scheme}://{host}:{origin.port}"


def _canonical_egress_url_prefix(
    value: object,
    *,
    error_code: str = "invalid_policy_preface",
) -> str:
    """Return one non-ambiguous exact-policy URL prefix."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 8_192
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in value
        )
    ):
        raise ProxyPolicyError(error_code)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ProxyPolicyError(error_code) from exc
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProxyPolicyError(error_code)
    port = (
        parsed_port
        if parsed_port is not None
        else (443 if scheme == "https" else 80)
    )
    rendered_host = (
        f"[{hostname}]"
        if ":" in hostname and not hostname.startswith("[")
        else hostname
    )
    try:
        origin = _origin_text(
            _origin_tuple(f"{scheme}://{rendered_host}:{port}")
        )
    except ProxyPolicyError as exc:
        raise ProxyPolicyError(error_code) from exc
    path = parsed.path or "/"
    if (
        not path.startswith("/")
        or "\\" in path
        or "//" in path
        or "{" in path
        or "}" in path
        or _INVALID_EGRESS_PERCENT_ESCAPE.search(path)
        or _INVALID_EGRESS_ENCODED_PATH.search(path)
        or any(
            re.fullmatch(r"\.{1,2}(?:;.*)?", component) is not None
            for component in path.split("/")
        )
    ):
        raise ProxyPolicyError(error_code)
    query = parsed.query
    if (
        "{" in query
        or "}" in query
        or ";" in query
        or _INVALID_EGRESS_PERCENT_ESCAPE.search(query)
        or re.search(
            r"%(?:25|23|0[0-9A-F]|1[0-9A-F]|7F)",
            query,
            re.IGNORECASE,
        )
    ):
        raise ProxyPolicyError(error_code)
    canonical = urlunsplit((
        scheme,
        urlsplit(origin).netloc,
        path,
        query,
        "",
    ))
    if canonical != value:
        raise ProxyPolicyError(error_code)
    return canonical


def _validated_signed_egress_policy(
    origins_raw: object,
    rules_raw: object,
    private_origins_raw: object,
    *,
    version: int = 2,
    budget_scope_sha256: object = None,
    call_id_sha256: object = None,
    limits_raw: object = None,
) -> SignedEgressPolicy:
    """Compile and cross-check one authenticated version-2/3 policy."""

    limits: PolicyBudgetLimits | None = None
    if version == 2:
        if any(
            value is not None
            for value in (
                budget_scope_sha256,
                call_id_sha256,
                limits_raw,
            )
        ):
            raise ProxyPolicyError("invalid_policy_preface")
    elif version == 3:
        if (
            not isinstance(budget_scope_sha256, str)
            or _SHA256_HEX_RE.fullmatch(budget_scope_sha256) is None
            or not isinstance(call_id_sha256, str)
            or _SHA256_HEX_RE.fullmatch(call_id_sha256) is None
            or not isinstance(limits_raw, dict)
            or set(limits_raw) != {
                "max_requests",
                "max_outbound_bytes",
                "max_response_wire_bytes",
            }
        ):
            raise ProxyPolicyError("invalid_policy_preface")
        request_limit = limits_raw.get("max_requests")
        outbound_limit = limits_raw.get("max_outbound_bytes")
        response_limit = limits_raw.get("max_response_wire_bytes")
        if (
            type(request_limit) is not int
            or not 1 <= request_limit <= MAX_REQUESTS_PER_SCOPE
            or type(outbound_limit) is not int
            or not 1 <= outbound_limit <= MAX_OUTBOUND_BYTES_PER_SCOPE
            or type(response_limit) is not int
            or not 1 <= response_limit <= MAX_RESPONSE_WIRE_BYTES_PER_SCOPE
        ):
            raise ProxyPolicyError("invalid_policy_preface")
        limits = PolicyBudgetLimits(
            max_requests=request_limit,
            max_outbound_bytes=outbound_limit,
            max_response_wire_bytes=response_limit,
        )
    else:
        raise ProxyPolicyError("invalid_policy_preface")

    if (
        not isinstance(origins_raw, list)
        or len(origins_raw) > MAX_ORIGIN_ALLOWLIST_ENTRIES
        or not isinstance(rules_raw, list)
        or len(rules_raw) > MAX_EXACT_EGRESS_RULES
        or not isinstance(private_origins_raw, list)
        or len(private_origins_raw)
        > MAX_ORIGIN_ALLOWLIST_ENTRIES
    ):
        raise ProxyPolicyError("invalid_policy_preface")

    ordered_origins: list[Origin] = []
    for value in origins_raw:
        if not isinstance(value, str):
            raise ProxyPolicyError("invalid_policy_preface")
        origin = _origin_tuple(value)
        if _origin_text(origin) != value or origin in ordered_origins:
            raise ProxyPolicyError("invalid_policy_preface")
        ordered_origins.append(origin)

    ordered_private: list[Origin] = []
    for value in private_origins_raw:
        if not isinstance(value, str):
            raise ProxyPolicyError("invalid_policy_preface")
        origin = _origin_tuple(value)
        if _origin_text(origin) != value or origin in ordered_private:
            raise ProxyPolicyError("invalid_policy_preface")
        ordered_private.append(origin)

    compiled_rules: list[ExactEgressRule] = []
    derived_origins: list[Origin] = []
    seen_rules: set[tuple[str, tuple[str, ...], bool]] = set()
    for raw_rule in rules_raw:
        keys = set(raw_rule) if isinstance(raw_rule, dict) else set()
        if (
            not isinstance(raw_rule, dict)
            or keys not in (
                {"methods", "url_prefix"},
                {"methods", "url_prefix", "query_exact"},
            )
            or not isinstance(raw_rule.get("methods"), list)
            or type(raw_rule.get("query_exact", False)) is not bool
        ):
            raise ProxyPolicyError("invalid_policy_preface")
        methods_raw = raw_rule["methods"]
        if (
            not methods_raw
            or len(methods_raw) > len(EGRESS_METHOD_ORDER)
            or any(
                not isinstance(method, str)
                or method not in EGRESS_METHODS
                for method in methods_raw
            )
            or len(set(methods_raw)) != len(methods_raw)
        ):
            raise ProxyPolicyError("invalid_policy_preface")
        canonical_methods = tuple(
            method
            for method in EGRESS_METHOD_ORDER
            if method in set(methods_raw)
        )
        prefix = _canonical_egress_url_prefix(
            raw_rule.get("url_prefix")
        )
        query_exact = bool(raw_rule.get("query_exact", False))
        coordinate = (prefix, canonical_methods, query_exact)
        if (
            methods_raw != list(canonical_methods)
            or coordinate in seen_rules
        ):
            raise ProxyPolicyError("invalid_policy_preface")
        seen_rules.add(coordinate)
        parsed = urlsplit(prefix)
        origin = _origin_tuple(
            f"{parsed.scheme}://{parsed.netloc}"
        )
        if origin not in derived_origins:
            derived_origins.append(origin)
        compiled_rules.append(ExactEgressRule(
            methods=frozenset(canonical_methods),
            origin=origin,
            path_prefix=parsed.path or "/",
            query_prefix=parsed.query,
            query_exact=query_exact,
        ))

    if (
        derived_origins != ordered_origins
        or any(origin not in ordered_origins for origin in ordered_private)
    ):
        raise ProxyPolicyError("invalid_policy_preface")
    return SignedEgressPolicy(
        origins=frozenset(ordered_origins),
        rules=tuple(compiled_rules),
        private_origins=frozenset(ordered_private),
        version=version,
        budget_scope_sha256=(
            budget_scope_sha256 if version == 3 else None
        ),
        call_id_sha256=call_id_sha256 if version == 3 else None,
        limits=limits,
    )


@dataclass(slots=True)
class _PolicyScopeState:
    limits: PolicyBudgetLimits
    requests: int
    outbound_bytes: int
    response_wire_bytes: int
    active: int
    last_activity: float


class PolicyScopeLedger:
    """Thread-safe, bounded cumulative accounting for signed v3 scopes."""

    def __init__(
        self,
        *,
        capacity: int = MAX_POLICY_SCOPE_ENTRIES,
        ttl_seconds: float = POLICY_SCOPE_TTL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        if (
            not 1 <= capacity <= _ABSOLUTE_MAX_POLICY_SCOPE_ENTRIES
            or not 0 < ttl_seconds
            <= _ABSOLUTE_MAX_POLICY_SCOPE_TTL_SECONDS
        ):
            raise ProxyPolicyError("invalid_policy_scope_ledger")
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._states: OrderedDict[str, _PolicyScopeState] = OrderedDict()
        # Active scopes must never block collection of an older inactive
        # scope.  Keep the reclaimable population in its own activity-ordered
        # index instead of scanning the full deployment ledger.
        self._inactive: OrderedDict[str, None] = OrderedDict()

    def _prune(self, now: float) -> None:
        # Inactive entries are ordered by the moment their final reservation
        # was released.  Reclaiming the expired prefix is proportional to the
        # entries removed and cannot be obstructed by a long-lived active
        # connection.
        while self._inactive:
            scope = next(iter(self._inactive))
            state = self._states.get(scope)
            if state is None:
                self._inactive.pop(scope, None)
                continue
            if state.active != 0:
                # Repair a stale secondary-index row defensively instead of
                # allowing it to obstruct every later reclaimable scope.
                self._inactive.pop(scope, None)
                continue
            if now - state.last_activity < self._ttl_seconds:
                break
            self._inactive.pop(scope, None)
            self._states.pop(scope, None)

    def admit(
        self,
        policy: SignedEgressPolicy,
    ) -> "PolicyBudgetReservation | None":
        if policy.version == 2:
            return None
        scope = policy.budget_scope_sha256
        limits = policy.limits
        if scope is None or limits is None:
            raise ProxyPolicyError("invalid_policy_preface")
        with self._lock:
            now = self._clock()
            self._prune(now)
            state = self._states.get(scope)
            if state is None:
                if len(self._states) >= self._capacity:
                    raise ProxyPolicyError(
                        "policy_scope_ledger_capacity_exceeded"
                    )
                state = _PolicyScopeState(
                    limits=limits,
                    requests=0,
                    outbound_bytes=0,
                    response_wire_bytes=0,
                    active=0,
                    last_activity=now,
                )
                self._states[scope] = state
            elif state.limits != limits:
                raise ProxyPolicyError("policy_scope_limits_mismatch")
            if state.requests >= state.limits.max_requests:
                raise ProxyPolicyError("policy_request_budget_exceeded")
            if state.active == 0:
                self._inactive.pop(scope, None)
            state.requests += 1
            state.active += 1
            state.last_activity = now
            self._states.move_to_end(scope)
        return PolicyBudgetReservation(self, scope)

    def _consume(
        self,
        scope: str,
        field: str,
        amount: int,
        error_code: str,
    ) -> None:
        if amount < 0:
            raise ProxyPolicyError("invalid_policy_budget_amount")
        with self._lock:
            state = self._states.get(scope)
            if state is None or state.active <= 0:
                raise ProxyPolicyError("policy_scope_reservation_lost")
            maximum = getattr(state.limits, f"max_{field}")
            current = getattr(state, field)
            if amount > maximum - current:
                raise ProxyPolicyError(error_code)
            setattr(state, field, current + amount)
            state.last_activity = self._clock()
            self._states.move_to_end(scope)

    def _release(self, scope: str) -> None:
        with self._lock:
            state = self._states.get(scope)
            if state is None or state.active <= 0:
                return
            state.active -= 1
            state.last_activity = self._clock()
            self._states.move_to_end(scope)
            if state.active == 0:
                self._inactive[scope] = None
                self._inactive.move_to_end(scope)


class PolicyBudgetReservation:
    """One admitted v3 connection's handle into its shared scope ledger."""

    def __init__(self, ledger: PolicyScopeLedger, scope: str) -> None:
        self._ledger = ledger
        self._scope = scope
        self._released = False

    def consume_outbound(self, amount: int) -> None:
        self._ledger._consume(
            self._scope,
            "outbound_bytes",
            amount,
            "policy_outbound_budget_exceeded",
        )

    def consume_response_wire(self, amount: int) -> None:
        self._ledger._consume(
            self._scope,
            "response_wire_bytes",
            amount,
            "policy_response_wire_budget_exceeded",
        )

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._ledger._release(self._scope)


def _request_policy_coordinate(
    forwarded: bytes,
    expected_origin: Origin,
) -> tuple[str, str, str]:
    """Parse one already-normalized origin-form request for policy matching."""

    header, separator, _body = forwarded.partition(b"\r\n\r\n")
    if not separator:
        raise ProxyPolicyError("invalid_request_line")
    first_line = header.partition(b"\r\n")[0]
    try:
        method_raw, target_raw, version_raw = first_line.split(b" ", 2)
        method = method_raw.decode("ascii", errors="strict")
        target = target_raw.decode("ascii", errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise ProxyPolicyError("invalid_request_line") from exc
    if (
        method not in EGRESS_METHODS
        or method_raw != method.encode("ascii")
        or version_raw not in {b"HTTP/1.0", b"HTTP/1.1"}
    ):
        raise ProxyPolicyError("request_method_not_allowed")
    if (
        len(target_raw) > MAX_HTTP_REQUEST_TARGET_BYTES
        or not target.startswith("/")
        or target.startswith("//")
        or "#" in target
    ):
        raise ProxyPolicyError("request_url_not_allowed")
    canonical = _canonical_egress_url_prefix(
        _origin_text(expected_origin) + target,
        error_code="request_url_not_allowed",
    )
    parsed = urlsplit(canonical)
    if parsed.query:
        fields = parsed.query.split("&")
        if len(fields) > MAX_HTTP_QUERY_FIELDS:
            raise ProxyPolicyError("request_query_too_large")
        for field in fields:
            key, separator, value = field.partition("=")
            if (
                len(key.encode("ascii")) > MAX_HTTP_QUERY_KEY_BYTES
                or (
                    separator
                    and len(value.encode("ascii"))
                    > MAX_HTTP_QUERY_VALUE_BYTES
                )
            ):
                raise ProxyPolicyError("request_query_too_large")
    return method, parsed.path or "/", parsed.query


def _authorize_exact_request(
    policy: SignedEgressPolicy,
    origin: Origin,
    forwarded: bytes,
) -> None:
    """Require a method/path/query match before any upstream connection."""

    method, path, query = _request_policy_coordinate(
        forwarded,
        origin,
    )
    for rule in policy.rules:
        if rule.origin != origin or method not in rule.methods:
            continue
        path_matches = (
            path.startswith(rule.path_prefix)
            if rule.path_prefix.endswith("/")
            else path == rule.path_prefix
        )
        if not path_matches:
            continue
        if rule.query_exact and query != rule.query_prefix:
            continue
        if (
            not rule.query_exact
            and rule.query_prefix
            and not query.startswith(rule.query_prefix)
        ):
            continue
        return
    raise ProxyPolicyError("request_url_not_allowed")


def _run_openssl(
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: float = 15.0,
) -> bytes:
    """Run the fixed certificate utility without a shell or ambient secrets."""

    if OPENSSL_BINARY != os.path.abspath(OPENSSL_BINARY):
        raise ProxyPolicyError("openssl_unavailable")
    try:
        completed = subprocess.run(
            [OPENSSL_BINARY, *arguments],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProxyPolicyError("openssl_unavailable") from exc
    if completed.returncode != 0:
        raise ProxyPolicyError("certificate_operation_failed")
    return bytes(completed.stdout)


def _validate_public_trust_directory(path: Path) -> Path:
    if not path.is_absolute() or "\x00" in str(path):
        raise ProxyPolicyError("invalid_public_trust_directory")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProxyPolicyError("public_trust_directory_unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or not metadata.st_mode & stat.S_IXGRP
        or metadata.st_mode & 0o007
    ):
        raise ProxyPolicyError("unsafe_public_trust_directory")
    return path


def _validate_private_trust_directory(path: Path) -> Path:
    if not path.is_absolute() or "\x00" in str(path):
        raise ProxyPolicyError("invalid_private_trust_directory")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProxyPolicyError(
            "private_trust_directory_unavailable"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ProxyPolicyError("unsafe_private_trust_directory")
    return path


def _read_secure_regular_file(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    maximum_bytes: int,
    error_code: str,
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != expected_uid
            or before.st_gid != expected_gid
            or stat.S_IMODE(before.st_mode) != expected_mode
            or not 1 <= before.st_size <= maximum_bytes
        ):
            raise ProxyPolicyError(error_code)
        content = bytearray()
        while len(content) <= maximum_bytes:
            chunk = os.read(
                descriptor,
                min(
                    COPY_CHUNK_BYTES,
                    maximum_bytes + 1 - len(content),
                ),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(content) != before.st_size
            or len(content) > maximum_bytes
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
        ):
            raise ProxyPolicyError(error_code)
        return bytes(content)
    except OSError as exc:
        raise ProxyPolicyError(error_code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_publish_public_file(
    path: Path,
    content: bytes,
    *,
    mode: int,
) -> None:
    parent = _validate_public_trust_directory(path.parent)
    if path.parent != parent or path.name not in {
        "ca.pem",
        "leaf.spki",
        "generation.json",
    }:
        raise ProxyPolicyError("invalid_public_trust_path")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise ProxyPolicyError("public_trust_publish_failed") from exc
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.geteuid()
    ):
        raise ProxyPolicyError("unsafe_public_trust_file")
    temporary = parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            # Persist the final reader-visible mode on the file itself before
            # the directory entry is committed. A directory fsync guarantees
            # the rename, but does not independently guarantee a chmod which
            # happened after the file's previous fsync.
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise ProxyPolicyError("public_trust_publish_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _trust_generation_id(
    ca_content: bytes,
    spki_content: bytes,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"chatds-egress-trust-generation-v1\x00")
    digest.update(ca_content)
    digest.update(b"\x00")
    digest.update(spki_content)
    return digest.hexdigest()


def _trust_generation_manifest(
    ca_content: bytes,
    spki_content: bytes,
) -> tuple[str, bytes]:
    generation_id = _trust_generation_id(
        ca_content,
        spki_content,
    )
    payload = {
        "version": TRUST_GENERATION_MANIFEST_VERSION,
        "generation_id": generation_id,
        "ca_file_sha256": _sha256_hex(ca_content),
        "leaf_spki_file_sha256": _sha256_hex(spki_content),
    }
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    return generation_id, encoded


def _parse_trust_generation_manifest(
    content: bytes,
) -> dict[str, str | int]:
    if not 1 <= len(content) <= MAX_TRUST_GENERATION_MANIFEST_BYTES:
        raise ProxyPolicyError("invalid_trust_generation_manifest")
    try:
        payload = json.loads(content.decode("ascii", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProxyPolicyError(
            "invalid_trust_generation_manifest"
        ) from exc
    expected_fields = {
        "version",
        "generation_id",
        "ca_file_sha256",
        "leaf_spki_file_sha256",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or payload.get("version") != TRUST_GENERATION_MANIFEST_VERSION
        or isinstance(payload.get("version"), bool)
        or any(
            not isinstance(payload.get(name), str)
            or _SHA256_HEX_RE.fullmatch(payload[name]) is None
            for name in (
                "generation_id",
                "ca_file_sha256",
                "leaf_spki_file_sha256",
            )
        )
    ):
        raise ProxyPolicyError("invalid_trust_generation_manifest")
    canonical = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    if content != canonical:
        raise ProxyPolicyError("invalid_trust_generation_manifest")
    return payload


def _spki_sha256_from_public_key(public_key_der: bytes) -> str:
    return base64.b64encode(
        hashlib.sha256(public_key_der).digest()
    ).decode("ascii")


def _certificate_spki_sha256(certificate_der: bytes) -> str:
    public_key_pem = _run_openssl(
        ["x509", "-inform", "DER", "-pubkey", "-noout"],
        input_bytes=certificate_der,
    )
    public_key_der = _run_openssl(
        ["pkey", "-pubin", "-outform", "DER"],
        input_bytes=public_key_pem,
    )
    return _spki_sha256_from_public_key(public_key_der)


class CertificateAuthority:
    """Persistent proxy-only interception keys with public trust metadata.

    The CA and shared leaf private keys live in a mode-0700 proxy-only volume
    and survive an ordinary proxy restart. Per-host certificates remain
    process-local and short-lived. The shared controller volume receives only
    the CA certificate, leaf SPKI, and a manifest committed last; it never
    receives a private key.
    """

    def __init__(
        self,
        *,
        public_directory: Path = PUBLIC_TRUST_DIRECTORY,
        private_directory: Path = PRIVATE_TRUST_DIRECTORY,
        runtime_parent: str | None = None,
    ) -> None:
        self.public_directory = _validate_public_trust_directory(
            public_directory
        )
        self.private_directory = _validate_private_trust_directory(
            private_directory
        )
        try:
            self.runtime_directory = Path(tempfile.mkdtemp(
                prefix="chatds-egress-ca-",
                dir=runtime_parent,
            ))
            os.chmod(self.runtime_directory, 0o700)
        except OSError as exc:
            raise ProxyPolicyError("certificate_runtime_unavailable") from exc
        try:
            protected_directories = (
                self.public_directory,
                self.private_directory,
                self.runtime_directory,
            )
            if any(
                left == right
                or left in right.parents
                or right in left.parents
                for index, left in enumerate(protected_directories)
                for right in protected_directories[index + 1 :]
            ):
                raise ProxyPolicyError(
                    "certificate_private_material_must_not_be_shared"
                )
            self.ca_key = self.private_directory / "ca.key"
            self.ca_certificate = self.private_directory / "ca.pem"
            self.leaf_key = self.private_directory / "leaf.key"
            self.leaf_request = self.runtime_directory / "leaf.csr"
            self._contexts: OrderedDict[
                str,
                tuple[ssl.SSLContext, float],
            ] = OrderedDict()
            self._lock = threading.RLock()
            self._initialize_private_material()
            self._create_leaf_request()
            self.leaf_spki = self._leaf_public_key_spki()
            ca_content = _read_secure_regular_file(
                self.ca_certificate,
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
                expected_mode=0o400,
                maximum_bytes=MAX_CA_CERTIFICATE_BYTES,
                error_code="unsafe_private_trust_material",
            )
            spki_content = (self.leaf_spki + "\n").encode("ascii")
            self.generation_id, manifest_content = (
                _trust_generation_manifest(
                    ca_content,
                    spki_content,
                )
            )
            self._publish_or_validate_public_material(
                ca_content,
                spki_content,
                manifest_content,
            )
        except BaseException:
            self.close()
            raise

    @property
    def public_ca_path(self) -> Path:
        return self.public_directory / "ca.pem"

    @property
    def public_spki_path(self) -> Path:
        return self.public_directory / "leaf.spki"

    @property
    def public_generation_path(self) -> Path:
        return self.public_directory / "generation.json"

    def _initialize_private_material(self) -> None:
        required = {"ca.key", "ca.pem", "leaf.key"}
        try:
            names = {entry.name for entry in self.private_directory.iterdir()}
        except OSError as exc:
            raise ProxyPolicyError(
                "private_trust_directory_unavailable"
            ) from exc
        public_material_exists = False
        for path in (
            self.public_ca_path,
            self.public_spki_path,
            self.public_generation_path,
        ):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProxyPolicyError(
                    "public_trust_material_unavailable"
                ) from exc
            public_material_exists = True
        if not names:
            if public_material_exists:
                raise ProxyPolicyError(
                    "private_trust_missing_for_public_generation"
                )
            self._generate_private_material()
        elif names != required:
            # A host crash can interrupt the very first private generation
            # between OpenSSL members. No public trust generation exists at
            # that point, so an owned, regular subset is safe to discard and
            # regenerate. Once any public member exists, incomplete private
            # state must remain fail-closed because its generation cannot be
            # authenticated.
            if not names < required or public_material_exists:
                raise ProxyPolicyError(
                    "incomplete_private_trust_material"
                )
            self._discard_unpublished_private_material(names)
            self._generate_private_material()
        try:
            self._validate_private_material()
        except ProxyPolicyError:
            # A power loss after OpenSSL creates the third pathname but before
            # its contents, chmod, or directory fsync leaves the complete name
            # set with an invalid final member. When no public trust member
            # exists there is still no committed generation, so a strictly
            # owned, regular set can be discarded and regenerated. Once even
            # one public member exists, retain the original failure and never
            # rotate or heal mismatched private authority implicitly.
            if public_material_exists or names != required:
                raise
            self._discard_unpublished_private_material(names)
            self._generate_private_material()
            self._validate_private_material()

    def _discard_unpublished_private_material(
        self,
        names: set[str],
    ) -> None:
        required = {"ca.key", "ca.pem", "leaf.key"}
        if not names or not names <= required:
            raise ProxyPolicyError(
                "unsafe_incomplete_private_trust_material"
            )
        for name in names:
            path = self.private_directory / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ProxyPolicyError(
                    "unsafe_incomplete_private_trust_material"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or metadata.st_nlink != 1
            ):
                raise ProxyPolicyError(
                    "unsafe_incomplete_private_trust_material"
                )
        try:
            for name in names:
                (self.private_directory / name).unlink()
            directory_descriptor = os.open(
                self.private_directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise ProxyPolicyError(
                "incomplete_private_trust_cleanup_failed"
            ) from exc

    def _generate_private_material(self) -> None:
        created_paths = (
            self.ca_key,
            self.ca_certificate,
            self.leaf_key,
        )
        try:
            _run_openssl([
                "genpkey",
                "-algorithm",
                "EC",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-out",
                str(self.ca_key),
            ])
            os.chmod(self.ca_key, 0o400)
            _run_openssl([
                "req",
                "-new",
                "-x509",
                "-key",
                str(self.ca_key),
                "-sha256",
                "-days",
                "3650",
                "-subj",
                "/CN=ChatDS Session Sandbox Interception CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-addext",
                "subjectKeyIdentifier=hash",
                "-out",
                str(self.ca_certificate),
            ])
            os.chmod(self.ca_certificate, 0o400)
            _run_openssl([
                "genpkey",
                "-algorithm",
                "EC",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-out",
                str(self.leaf_key),
            ])
            os.chmod(self.leaf_key, 0o400)
            # File data must reach stable storage before the directory entry
            # transaction is committed. A directory fsync alone can preserve
            # all three names while losing the final key/certificate bytes.
            for path in created_paths:
                descriptor = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    metadata = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != os.geteuid()
                        or metadata.st_gid != os.getegid()
                        or metadata.st_nlink != 1
                        or stat.S_IMODE(metadata.st_mode) != 0o400
                        or metadata.st_size <= 0
                    ):
                        raise ProxyPolicyError(
                            "unsafe_private_trust_material"
                        )
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            directory_descriptor = os.open(
                self.private_directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            for path in created_paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise

    def _validate_private_material(self) -> None:
        for path in (
            self.ca_key,
            self.ca_certificate,
            self.leaf_key,
        ):
            _read_secure_regular_file(
                path,
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
                expected_mode=0o400,
                maximum_bytes=MAX_CA_CERTIFICATE_BYTES,
                error_code="unsafe_private_trust_material",
            )
        _run_openssl([
            "pkey",
            "-in",
            str(self.ca_key),
            "-check",
            "-noout",
        ])
        _run_openssl([
            "pkey",
            "-in",
            str(self.leaf_key),
            "-check",
            "-noout",
        ])
        _run_openssl([
            "x509",
            "-in",
            str(self.ca_certificate),
            "-checkend",
            "86400",
            "-noout",
        ])
        _run_openssl([
            "verify",
            "-CAfile",
            str(self.ca_certificate),
            str(self.ca_certificate),
        ])
        certificate_public_key = _run_openssl([
            "x509",
            "-in",
            str(self.ca_certificate),
            "-pubkey",
            "-noout",
        ])
        private_public_key = _run_openssl([
            "pkey",
            "-in",
            str(self.ca_key),
            "-pubout",
        ])
        if not hmac.compare_digest(
            certificate_public_key,
            private_public_key,
        ):
            raise ProxyPolicyError("private_ca_key_mismatch")

    def _create_leaf_request(self) -> None:
        _run_openssl([
            "req",
            "-new",
            "-key",
            str(self.leaf_key),
            "-subj",
            "/CN=ChatDS Session Sandbox Intercepted Origin",
            "-out",
            str(self.leaf_request),
        ])
        os.chmod(self.leaf_request, 0o400)

    def _publish_or_validate_public_material(
        self,
        ca_content: bytes,
        spki_content: bytes,
        manifest_content: bytes,
    ) -> None:
        paths = (
            self.public_ca_path,
            self.public_spki_path,
            self.public_generation_path,
        )
        present: list[bool] = []
        for path in paths:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                present.append(False)
                continue
            except OSError as exc:
                raise ProxyPolicyError(
                    "public_trust_material_unavailable"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise ProxyPolicyError("unsafe_public_trust_file")
            present.append(True)
        if not any(present):
            # Publish data files first and the manifest last. Readers treat
            # the manifest as the commit marker and verify it before and after
            # copying, so a crash can only produce a typed fail-closed state.
            _atomic_publish_public_file(
                self.public_ca_path,
                ca_content,
                mode=0o440,
            )
            _atomic_publish_public_file(
                self.public_spki_path,
                spki_content,
                mode=0o440,
            )
            _atomic_publish_public_file(
                self.public_generation_path,
                manifest_content,
                mode=0o440,
            )
            return
        if not all(present):
            # The initial public publish is an ordered three-member
            # transaction: CA, leaf SPKI, then the manifest commit marker.
            # A crash may therefore leave either of the two valid prefixes.
            # Recover only when every present prefix member exactly matches
            # the complete, validated private authority loaded above. A
            # manifest without both data members, any out-of-order member, or
            # any mismatch remains fail-closed.
            ca_present, spki_present, manifest_present = present
            if (
                manifest_present
                or (spki_present and not ca_present)
            ):
                raise ProxyPolicyError("mixed_public_trust_generation")
            expected_gid = self.public_directory.stat().st_gid
            if ca_present:
                observed_ca = _read_secure_regular_file(
                    self.public_ca_path,
                    expected_uid=os.geteuid(),
                    expected_gid=expected_gid,
                    expected_mode=0o440,
                    maximum_bytes=MAX_CA_CERTIFICATE_BYTES,
                    error_code="unsafe_public_trust_file",
                )
                if not hmac.compare_digest(observed_ca, ca_content):
                    raise ProxyPolicyError(
                        "mixed_public_trust_generation"
                    )
            if spki_present:
                observed_spki = _read_secure_regular_file(
                    self.public_spki_path,
                    expected_uid=os.geteuid(),
                    expected_gid=expected_gid,
                    expected_mode=0o440,
                    maximum_bytes=MAX_SPKI_FILE_BYTES,
                    error_code="unsafe_public_trust_file",
                )
                if not hmac.compare_digest(
                    observed_spki,
                    spki_content,
                ):
                    raise ProxyPolicyError(
                        "mixed_public_trust_generation"
                    )
            if not ca_present:
                _atomic_publish_public_file(
                    self.public_ca_path,
                    ca_content,
                    mode=0o440,
                )
            if not spki_present:
                _atomic_publish_public_file(
                    self.public_spki_path,
                    spki_content,
                    mode=0o440,
                )
            _atomic_publish_public_file(
                self.public_generation_path,
                manifest_content,
                mode=0o440,
            )

        expected_gid = self.public_directory.stat().st_gid
        observed_ca = _read_secure_regular_file(
            self.public_ca_path,
            expected_uid=os.geteuid(),
            expected_gid=expected_gid,
            expected_mode=0o440,
            maximum_bytes=MAX_CA_CERTIFICATE_BYTES,
            error_code="unsafe_public_trust_file",
        )
        observed_spki = _read_secure_regular_file(
            self.public_spki_path,
            expected_uid=os.geteuid(),
            expected_gid=expected_gid,
            expected_mode=0o440,
            maximum_bytes=MAX_SPKI_FILE_BYTES,
            error_code="unsafe_public_trust_file",
        )
        observed_manifest = _read_secure_regular_file(
            self.public_generation_path,
            expected_uid=os.geteuid(),
            expected_gid=expected_gid,
            expected_mode=0o440,
            maximum_bytes=MAX_TRUST_GENERATION_MANIFEST_BYTES,
            error_code="unsafe_public_trust_file",
        )
        parsed = _parse_trust_generation_manifest(
            observed_manifest
        )
        if (
            parsed["ca_file_sha256"] != _sha256_hex(observed_ca)
            or parsed["leaf_spki_file_sha256"]
            != _sha256_hex(observed_spki)
            or parsed["generation_id"]
            != _trust_generation_id(observed_ca, observed_spki)
        ):
            raise ProxyPolicyError("mixed_public_trust_generation")
        if (
            not hmac.compare_digest(observed_ca, ca_content)
            or not hmac.compare_digest(observed_spki, spki_content)
            or not hmac.compare_digest(
                observed_manifest,
                manifest_content,
            )
        ):
            raise ProxyPolicyError("public_trust_generation_mismatch")

    def _leaf_public_key_spki(self) -> str:
        public_key_der = _run_openssl([
            "pkey",
            "-in",
            str(self.leaf_key),
            "-pubout",
            "-outform",
            "DER",
        ])
        value = _spki_sha256_from_public_key(public_key_der)
        if _SPKI_SHA256_RE.fullmatch(value) is None:
            raise ProxyPolicyError("invalid_interception_spki")
        return value

    def _issue_certificate(self, host: str) -> Path:
        canonical_host = _normalized_host(host)
        digest = hashlib.sha256(
            canonical_host.encode("ascii")
        ).hexdigest()
        extension_path = self.runtime_directory / f"{digest}.ext"
        certificate_path = self.runtime_directory / f"{digest}.pem"
        chain_path = self.runtime_directory / f"{digest}.chain.pem"
        try:
            address = ipaddress.ip_address(canonical_host)
        except ValueError:
            subject_alt_name = f"DNS:{canonical_host}"
        else:
            subject_alt_name = f"IP:{address.compressed}"
        extension_text = (
            "[chatds_leaf]\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n"
            f"subjectAltName={subject_alt_name}\n"
        )
        try:
            # The private runtime keeps issued material read-only after it has
            # been loaded into an SSLContext.  A scheduled refresh therefore
            # has to unlink the prior files before OpenSSL can create the new
            # certificate and before Python can write the replacement chain.
            # server_context() holds the CA lock while issuing, so there is no
            # concurrent reader of these paths.
            for existing_path in (certificate_path, chain_path):
                try:
                    existing_path.unlink()
                except FileNotFoundError:
                    pass
            extension_path.write_text(
                extension_text,
                encoding="ascii",
                errors="strict",
            )
            os.chmod(extension_path, 0o400)
            _run_openssl([
                "x509",
                "-req",
                "-in",
                str(self.leaf_request),
                "-CA",
                str(self.ca_certificate),
                "-CAkey",
                str(self.ca_key),
                "-set_serial",
                "0x" + secrets.token_hex(16),
                "-days",
                "1",
                "-sha256",
                "-extfile",
                str(extension_path),
                "-extensions",
                "chatds_leaf",
                "-out",
                str(certificate_path),
            ])
            chain_path.write_bytes(
                certificate_path.read_bytes()
                + self.ca_certificate.read_bytes()
            )
            os.chmod(certificate_path, 0o400)
            os.chmod(chain_path, 0o400)
            return chain_path
        except (OSError, UnicodeError) as exc:
            raise ProxyPolicyError(
                "interception_certificate_issue_failed"
            ) from exc
        finally:
            try:
                extension_path.unlink()
            except FileNotFoundError:
                pass

    def server_context(self, host: str) -> ssl.SSLContext:
        canonical_host = _normalized_host(host)
        with self._lock:
            cached = self._contexts.get(canonical_host)
            if cached is not None:
                cached_context, issued_at = cached
                if (
                    time.monotonic() - issued_at
                    < INTERCEPTION_CERTIFICATE_REFRESH_SECONDS
                ):
                    self._contexts.move_to_end(canonical_host)
                    return cached_context
                self._contexts.pop(canonical_host, None)
            chain_path = self._issue_certificate(canonical_host)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols(["http/1.1"])
            context.load_cert_chain(
                certfile=str(chain_path),
                keyfile=str(self.leaf_key),
            )

            def validate_sni(
                _socket: ssl.SSLSocket,
                server_name: str | None,
                _context: ssl.SSLContext,
            ) -> None:
                try:
                    address = ipaddress.ip_address(canonical_host)
                except ValueError:
                    if (
                        server_name is None
                        or _normalized_host(server_name)
                        != canonical_host
                    ):
                        raise ssl.SSLError("unrecognized_name")
                else:
                    if server_name is not None:
                        raise ssl.SSLError(
                            "SNI is forbidden for an IP-literal origin"
                        )

            context.set_servername_callback(validate_sni)
            self._contexts[canonical_host] = (
                context,
                time.monotonic(),
            )
            while len(self._contexts) > MAX_INTERCEPTION_CERTIFICATES:
                old_host, _old_context = self._contexts.popitem(last=False)
                old_digest = hashlib.sha256(
                    old_host.encode("ascii")
                ).hexdigest()
                for suffix in (".pem", ".chain.pem"):
                    try:
                        (
                            self.runtime_directory
                            / f"{old_digest}{suffix}"
                        ).unlink()
                    except FileNotFoundError:
                        pass
            return context

    def close(self) -> None:
        runtime = getattr(self, "runtime_directory", None)
        if isinstance(runtime, Path):
            shutil.rmtree(runtime, ignore_errors=True)


def _parse_upstream_tls_pins(
    value: str | None = None,
) -> dict[Origin, frozenset[str]]:
    raw = (
        os.environ.get("SKILL_EGRESS_UPSTREAM_TLS_SPKI_PINS", "")
        if value is None
        else value
    )
    if not raw.strip():
        return {}
    if len(raw.encode("utf-8", errors="strict")) > 64 * 1024:
        raise ProxyPolicyError("invalid_upstream_tls_spki_pins")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProxyPolicyError("invalid_upstream_tls_spki_pins") from exc
    if (
        not isinstance(parsed, dict)
        or len(parsed) > MAX_UPSTREAM_TLS_PIN_ENTRIES
    ):
        raise ProxyPolicyError("invalid_upstream_tls_spki_pins")
    result: dict[Origin, frozenset[str]] = {}
    for raw_origin, raw_pins in parsed.items():
        if (
            not isinstance(raw_origin, str)
            or not isinstance(raw_pins, list)
            or not 1 <= len(raw_pins) <= MAX_UPSTREAM_TLS_PINS_PER_ORIGIN
        ):
            raise ProxyPolicyError("invalid_upstream_tls_spki_pins")
        origin = _origin_tuple(raw_origin)
        if (
            origin.scheme != "https"
            or _origin_text(origin) != raw_origin
            or origin in result
        ):
            raise ProxyPolicyError("invalid_upstream_tls_spki_pins")
        pins: set[str] = set()
        for pin in raw_pins:
            if (
                not isinstance(pin, str)
                or _SPKI_SHA256_RE.fullmatch(pin) is None
                or pin in pins
            ):
                raise ProxyPolicyError("invalid_upstream_tls_spki_pins")
            pins.add(pin)
        result[origin] = frozenset(pins)
    return result


def _parse_legacy_private_tls_pins(
    value: str | None = None,
) -> frozenset[str]:
    """Parse the pre-existing Chromium pin list without granting authority.

    These pins are considered only after ``AddressPolicy`` has already
    established request-scoped exact-origin authority, an exact deployment
    private-origin grant, and the literal/CIDR address boundary.  They cannot
    turn a public or otherwise unauthorized destination into an allowed one.
    """

    raw = (
        os.environ.get("BROWSER_TLS_SPKI_ALLOWLIST", "")
        if value is None
        else value
    )
    if not raw.strip():
        return frozenset()
    try:
        encoded = raw.encode("ascii", errors="strict")
    except UnicodeError as exc:
        raise ProxyPolicyError(
            "invalid_legacy_private_tls_spki_pins"
        ) from exc
    if len(encoded) > 8 * 1024:
        raise ProxyPolicyError("invalid_legacy_private_tls_spki_pins")
    values = [item.strip() for item in raw.split(",")]
    if (
        not 1 <= len(values) <= MAX_UPSTREAM_TLS_PINS_PER_ORIGIN
        or any(not item for item in values)
    ):
        raise ProxyPolicyError("invalid_legacy_private_tls_spki_pins")
    pins: set[str] = set()
    for value_item in values:
        candidate = (
            value_item + "="
            if len(value_item) == 43
            else value_item
        )
        if (
            _SPKI_SHA256_RE.fullmatch(candidate) is None
            or candidate in pins
        ):
            raise ProxyPolicyError(
                "invalid_legacy_private_tls_spki_pins"
            )
        pins.add(candidate)
    return frozenset(pins)


class UpstreamTlsPolicy:
    """Establish HTTP/1.1 TLS to one already-resolved pinned endpoint."""

    def __init__(
        self,
        pins: dict[Origin, frozenset[str]] | None = None,
        *,
        legacy_private_pins: frozenset[str] | None = None,
    ) -> None:
        self.pins = (
            _parse_upstream_tls_pins()
            if pins is None
            else {
                _normalized_origin(
                    origin.scheme,
                    origin.host,
                    origin.port,
                ): frozenset(values)
                for origin, values in pins.items()
            }
        )
        if legacy_private_pins is None:
            self.legacy_private_pins = (
                _parse_legacy_private_tls_pins()
            )
        else:
            explicit_legacy_pins = frozenset(legacy_private_pins)
            if (
                len(explicit_legacy_pins)
                > MAX_UPSTREAM_TLS_PINS_PER_ORIGIN
                or any(
                    not isinstance(pin, str)
                    or _SPKI_SHA256_RE.fullmatch(pin) is None
                    for pin in explicit_legacy_pins
                )
            ):
                raise ProxyPolicyError(
                    "invalid_legacy_private_tls_spki_pins"
                )
            self.legacy_private_pins = explicit_legacy_pins

    def authorize(self, destination: Destination) -> None:
        origin = Origin(
            destination.scheme,
            destination.host,
            destination.port,
        )
        if (
            destination.private_grant
            and not self.pins.get(origin)
            and not self.legacy_private_pins
        ):
            raise ProxyPolicyError("upstream_tls_spki_pin_required")

    def wrap(
        self,
        raw_socket: socket.socket,
        destination: Destination,
    ) -> ssl.SSLSocket:
        origin = Origin(
            destination.scheme,
            destination.host,
            destination.port,
        )
        pins = self.pins.get(origin)
        if not pins and destination.private_grant:
            pins = self.legacy_private_pins
        self.authorize(destination)
        if pins:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context(
                purpose=ssl.Purpose.SERVER_AUTH
            )
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
        # Some TLS 1.3 origins require the RFC 8446 PHA extension in the
        # ClientHello.  No client certificate is loaded here, so advertising
        # support neither grants client identity nor relaxes peer validation.
        if hasattr(context, "post_handshake_auth"):
            context.post_handshake_auth = True
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(["http/1.1"])
        raw_socket.settimeout(TLS_HANDSHAKE_TIMEOUT_SECONDS)
        try:
            wrapped = context.wrap_socket(
                raw_socket,
                server_hostname=destination.host,
            )
        except (OSError, ssl.SSLError) as exc:
            raise ProxyPolicyError("upstream_tls_verification_failed") from exc
        try:
            negotiated = wrapped.selected_alpn_protocol()
            if negotiated not in {None, "http/1.1"}:
                raise ProxyPolicyError("upstream_http2_not_allowed")
            if pins:
                certificate = wrapped.getpeercert(binary_form=True)
                if (
                    not certificate
                    or _certificate_spki_sha256(certificate) not in pins
                ):
                    raise ProxyPolicyError(
                        "upstream_tls_spki_pin_mismatch"
                    )
            return wrapped
        except BaseException:
            wrapped.close()
            raise


def _policy_auth_key() -> bytes:
    token = os.environ.get("SKILL_EGRESS_POLICY_TOKEN", "")
    try:
        encoded = token.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ProxyPolicyError("policy_authentication_unavailable") from exc
    if not 32 <= len(encoded) <= 4_096:
        raise ProxyPolicyError("policy_authentication_unavailable")
    # The executor token also authenticates its private process protocol.
    # Derive a purpose-specific sub-key so a proxy preface can never be
    # replayed as, or disclose a verifier for, that independent protocol.
    return hmac.new(
        encoded,
        POLICY_KEY_DERIVATION_LABEL,
        hashlib.sha256,
    ).digest()


@dataclass(slots=True)
class _ReadDeadline:
    """One receive budget with independent absolute and idle cutoffs."""

    absolute_deadline: float
    idle_timeout_seconds: float
    idle_deadline: float

    @classmethod
    def start(
        cls,
        *,
        absolute_timeout_seconds: float,
        idle_timeout_seconds: float,
    ) -> "_ReadDeadline":
        if (
            absolute_timeout_seconds <= 0
            or idle_timeout_seconds <= 0
        ):
            raise ProxyPolicyError("invalid_request_read_deadline")
        now = time.monotonic()
        return cls(
            absolute_deadline=now + absolute_timeout_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
            idle_deadline=now + idle_timeout_seconds,
        )

    def recv(
        self,
        connection: socket.socket,
        size: int,
        *,
        error_code: str,
    ) -> bytes:
        if size <= 0:
            return b""
        now = time.monotonic()
        remaining = min(
            self.absolute_deadline - now,
            self.idle_deadline - now,
        )
        if remaining <= 0:
            raise ProxyPolicyError(error_code)
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(size)
        except (socket.timeout, TimeoutError) as exc:
            raise ProxyPolicyError(error_code) from exc
        if chunk:
            self.idle_deadline = (
                time.monotonic() + self.idle_timeout_seconds
            )
        return chunk


def _read_policy_preface(
    connection: socket.socket,
    *,
    expected_trust_generation: str,
) -> tuple[SignedEgressPolicy, bytes]:
    if _SHA256_HEX_RE.fullmatch(expected_trust_generation) is None:
        raise ProxyPolicyError("policy_trust_generation_unavailable")
    deadline = _ReadDeadline.start(
        absolute_timeout_seconds=(
            POLICY_PREFACE_ABSOLUTE_READ_TIMEOUT_SECONDS
        ),
        idle_timeout_seconds=HTTP_READ_IDLE_TIMEOUT_SECONDS,
    )
    data = bytearray()
    while b"\n" not in data:
        chunk = deadline.recv(
            connection,
            min(
                COPY_CHUNK_BYTES,
                MAX_POLICY_PREFACE_BYTES - len(data),
            ),
            error_code="policy_preface_read_timeout",
        )
        if not chunk:
            break
        data.extend(chunk)
        if len(data) >= MAX_POLICY_PREFACE_BYTES and b"\n" not in data:
            raise ProxyPolicyError("policy_preface_too_large")
    if b"\n" not in data:
        raise ProxyPolicyError("incomplete_policy_preface")
    raw_line, remainder = bytes(data).split(b"\n", 1)
    if not raw_line.startswith(POLICY_PREFACE_PREFIX):
        raise ProxyPolicyError("authenticated_policy_preface_required")
    try:
        payload = json.loads(
            raw_line[len(POLICY_PREFACE_PREFIX):].decode("utf-8")
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProxyPolicyError("invalid_policy_preface") from exc
    base_fields = {
        "version",
        "expires_unix",
        "nonce",
        "origins",
        "egress_rules",
        "private_origins",
        "trust_generation",
        "auth_hmac",
    }
    if not isinstance(payload, dict):
        raise ProxyPolicyError("invalid_policy_preface")
    version = payload.get("version")
    if type(version) is not int or version not in {2, 3}:
        raise ProxyPolicyError("invalid_policy_preface")
    v3_fields = {
        "budget_scope_sha256",
        "call_id_sha256",
        "limits",
    }
    expected_fields = (
        base_fields | v3_fields if version == 3 else base_fields
    )
    if set(payload) != expected_fields:
        raise ProxyPolicyError("invalid_policy_preface")
    expires = payload.get("expires_unix")
    nonce = payload.get("nonce")
    auth_hmac = payload.get("auth_hmac")
    origins_raw = payload.get("origins")
    egress_rules_raw = payload.get("egress_rules")
    private_origins_raw = payload.get("private_origins")
    trust_generation = payload.get("trust_generation")
    now = int(time.time())
    if (
        isinstance(expires, bool)
        or not isinstance(expires, int)
        or expires < now - POLICY_CLOCK_SKEW_SECONDS
        or expires > now + MAX_POLICY_TTL_SECONDS + POLICY_CLOCK_SKEW_SECONDS
        or not isinstance(nonce, str)
        or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        or not isinstance(auth_hmac, str)
        or re.fullmatch(r"[0-9a-f]{64}", auth_hmac) is None
        or not isinstance(origins_raw, list)
        or len(origins_raw) > MAX_ORIGIN_ALLOWLIST_ENTRIES
        or not isinstance(egress_rules_raw, list)
        or len(egress_rules_raw) > MAX_EXACT_EGRESS_RULES
        or not isinstance(private_origins_raw, list)
        or len(private_origins_raw)
        > MAX_ORIGIN_ALLOWLIST_ENTRIES
        or not isinstance(trust_generation, str)
        or _SHA256_HEX_RE.fullmatch(trust_generation) is None
    ):
        raise ProxyPolicyError("invalid_policy_preface")
    unsigned = {
        "version": payload["version"],
        "expires_unix": expires,
        "nonce": nonce,
        "origins": origins_raw,
        "egress_rules": egress_rules_raw,
        "private_origins": private_origins_raw,
        "trust_generation": trust_generation,
    }
    if version == 3:
        unsigned.update({
            "budget_scope_sha256": payload["budget_scope_sha256"],
            "call_id_sha256": payload["call_id_sha256"],
            "limits": payload["limits"],
        })
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_hmac = hmac.new(
        _policy_auth_key(),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(auth_hmac, expected_hmac):
        raise ProxyPolicyError("policy_authentication_failed")
    if not hmac.compare_digest(
        trust_generation,
        expected_trust_generation,
    ):
        raise ProxyPolicyError("policy_trust_generation_mismatch")
    if (
        REQUIRE_POLICY_V3
        and version == 2
        and bool(egress_rules_raw)
    ):
        raise ProxyPolicyError("egress_policy_upgrade_required")
    return (
        _validated_signed_egress_policy(
            origins_raw,
            egress_rules_raw,
            private_origins_raw,
            version=version,
            budget_scope_sha256=payload.get(
                "budget_scope_sha256"
            ),
            call_id_sha256=payload.get("call_id_sha256"),
            limits_raw=payload.get("limits"),
        ),
        remainder,
    )


class AddressPolicy:
    """Resolve one destination and pin it to an allowed address."""

    def __init__(
        self,
        *,
        public_ports: tuple[int, ...] | None = None,
        private_origins: tuple[str, ...] | None = None,
        private_cidrs: tuple[str, ...] | None = None,
    ) -> None:
        if public_ports is None:
            raw_ports = _split_csv(
                os.environ.get("SKILL_EGRESS_PUBLIC_PORTS", "80,443")
            )
            public_ports = tuple(int(item) for item in raw_ports)
        if (
            not public_ports
            or any(
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
                for port in public_ports
            )
        ):
            raise ProxyPolicyError("invalid_public_port_policy")
        self.public_ports = frozenset(public_ports)
        raw_origins = (
            private_origins
            if private_origins is not None
            else _split_csv(
                os.environ.get("SKILL_EGRESS_PRIVATE_ORIGIN_ALLOWLIST", "")
            )
        )
        self.private_origins = frozenset(_origin_tuple(item) for item in raw_origins)
        raw_cidrs = (
            private_cidrs
            if private_cidrs is not None
            else _split_csv(
                os.environ.get("SKILL_EGRESS_PRIVATE_CIDR_ALLOWLIST", "")
            )
        )
        try:
            self.private_cidrs = tuple(
                ipaddress.ip_network(item, strict=True) for item in raw_cidrs
            )
        except ValueError as exc:
            raise ProxyPolicyError("invalid_private_cidr_allowlist") from exc

    def resolve(
        self,
        scheme: str,
        host: str,
        port: int,
        *,
        origin_allowlist: (
            Iterable[Origin | tuple[str, str, int]] | None
        ) = None,
        signed_private_origins: (
            Iterable[Origin | tuple[str, str, int]] | None
        ) = None,
    ) -> Destination:
        """Authorize, resolve, classify, and pin one exact request origin.

        Exact request authority is checked before public-port policy and
        before DNS. A private-address exception requires the exact origin in
        both the deployment allowlist and the signed per-execution private
        projection; neither side grants request authority by itself.
        """

        origin = _normalized_origin(scheme, host, port)
        allowed_origins = normalize_origin_allowlist(origin_allowlist)
        if origin not in allowed_origins:
            raise ProxyPolicyError("destination_origin_not_allowed")

        signed_private = normalize_origin_allowlist(
            signed_private_origins
        )
        if any(item not in allowed_origins for item in signed_private):
            raise ProxyPolicyError(
                "signed_private_origin_not_allowed"
            )
        private_grant = (
            origin in self.private_origins
            and origin in signed_private
        )
        if not private_grant and port not in self.public_ports:
            raise ProxyPolicyError("destination_port_not_allowed")

        try:
            records = socket.getaddrinfo(
                origin.host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except (socket.timeout, TimeoutError) as exc:
            raise ProxyTransportTimeoutError(
                "destination_dns_timeout"
            ) from exc
        except socket.gaierror as exc:
            raise ProxyTransportError("destination_dns_failed") from exc
        candidates: list[tuple[int, str]] = []
        seen: set[tuple[int, str]] = set()
        for family, socktype, proto, _canonname, sockaddr in records:
            if (
                family not in {socket.AF_INET, socket.AF_INET6}
                or socktype != socket.SOCK_STREAM
                or proto not in {0, socket.IPPROTO_TCP}
            ):
                continue
            address = str(sockaddr[0])
            key = (family, address)
            if key not in seen:
                candidates.append(key)
                seen.add(key)
        if not candidates:
            raise ProxyTransportError("destination_dns_empty")

        classifications = [ipaddress.ip_address(address) for _, address in candidates]
        if not private_grant and any(
            not _is_public_unicast(address) for address in classifications
        ):
            # Reject a mixed public/private answer rather than selecting only
            # the public member.  This closes DNS-rebinding and split-horizon
            # ambiguity at the resolution boundary.
            raise ProxyPolicyError("destination_address_not_public")
        if private_grant:
            try:
                literal_private_address = ipaddress.ip_address(origin.host)
            except ValueError:
                literal_private_address = None
            if literal_private_address is not None:
                # A literal origin already supplies the narrowest possible
                # address authority. Pin every resolver record to that exact
                # value; requiring a redundant /32 or /128 cannot add a
                # rebinding defense.
                if any(
                    address != literal_private_address
                    for address in classifications
                ):
                    raise ProxyPolicyError(
                        "destination_address_outside_private_literal_origin"
                    )
            elif (
                not self.private_cidrs
                or any(
                    not any(address in network for network in self.private_cidrs)
                    for address in classifications
                )
            ):
                # Hostname grants need a second deployment-owned address
                # boundary so split DNS/rebinding cannot redirect an allowed
                # name to metadata or another internal segment.
                raise ProxyPolicyError(
                    "destination_address_outside_private_cidr_allowlist"
                )
            selected_family, selected_address = candidates[0]
        else:
            selected_family, selected_address = next(
                (family, address)
                for (family, address), classified in zip(candidates, classifications)
                if _is_public_unicast(classified)
            )
        return Destination(
            scheme=origin.scheme,
            host=origin.host,
            port=port,
            address=selected_address,
            family=selected_family,
            private_grant=private_grant,
        )


def _read_headers(
    connection: socket.socket,
    *,
    initial: bytes = b"",
) -> bytes:
    deadline = _ReadDeadline.start(
        absolute_timeout_seconds=(
            HTTP_HEADER_ABSOLUTE_READ_TIMEOUT_SECONDS
        ),
        idle_timeout_seconds=HTTP_READ_IDLE_TIMEOUT_SECONDS,
    )
    data = bytearray(initial)
    if len(data) > MAX_HEADER_BYTES:
        raise ProxyPolicyError("request_headers_too_large")
    while b"\r\n\r\n" not in data:
        chunk = deadline.recv(
            connection,
            min(COPY_CHUNK_BYTES, MAX_HEADER_BYTES - len(data)),
            error_code="request_headers_read_timeout",
        )
        if not chunk:
            break
        data.extend(chunk)
        if len(data) >= MAX_HEADER_BYTES and b"\r\n\r\n" not in data:
            raise ProxyPolicyError("request_headers_too_large")
    if b"\r\n\r\n" not in data:
        raise ProxyPolicyError("incomplete_request_headers")
    return bytes(data)


def _parse_connect_target(value: str) -> tuple[str, int]:
    if value.startswith("["):
        close = value.find("]")
        if close <= 1 or close + 1 >= len(value) or value[close + 1] != ":":
            raise ProxyPolicyError("invalid_connect_target")
        host = value[1:close]
        port_text = value[close + 2 :]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator or not host:
            raise ProxyPolicyError("invalid_connect_target")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ProxyPolicyError("invalid_connect_target") from exc
    if not 1 <= port <= 65535:
        raise ProxyPolicyError("invalid_connect_target")
    return host, port


def _parse_http_host_authority(
    value: bytes,
    *,
    scheme: str = "http",
) -> Origin:
    """Parse one Host field as an exact HTTP origin authority."""

    if scheme not in {"http", "https"}:
        raise ProxyPolicyError("invalid_http_host_header")
    default_port = 443 if scheme == "https" else 80
    try:
        rendered = value.strip(b" \t").decode("ascii")
    except UnicodeError as exc:
        raise ProxyPolicyError("invalid_http_host_header") from exc
    if (
        not rendered
        or any(char in rendered for char in "/?#@,")
        or any(char.isspace() for char in rendered)
    ):
        raise ProxyPolicyError("invalid_http_host_header")
    if rendered.startswith("["):
        close = rendered.find("]")
        if close <= 1:
            raise ProxyPolicyError("invalid_http_host_header")
        host = rendered[1:close]
        suffix = rendered[close + 1 :]
        if not suffix:
            port = default_port
        elif suffix.startswith(":") and suffix[1:].isdigit():
            port = int(suffix[1:])
        else:
            raise ProxyPolicyError("invalid_http_host_header")
    else:
        if rendered.count(":") > 1:
            raise ProxyPolicyError("invalid_http_host_header")
        host, separator, port_text = rendered.rpartition(":")
        if separator:
            if not host or not port_text.isdigit():
                raise ProxyPolicyError("invalid_http_host_header")
            port = int(port_text)
        else:
            host = rendered
            port = default_port
    return _normalized_origin(scheme, host, port)


def _canonical_http_host_header(origin: Origin) -> bytes:
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    default_port = 443 if origin.scheme == "https" else 80
    if origin.port != default_port:
        host += f":{origin.port}"
    return host.encode("ascii")


def _validated_header_parts(line: bytes) -> tuple[bytes, bytes]:
    name, separator, value = line.partition(b":")
    if (
        not separator
        or len(name) > MAX_HTTP_REQUEST_HEADER_NAME_BYTES
        or len(value) > MAX_HTTP_REQUEST_HEADER_VALUE_BYTES
        or _HTTP_HEADER_NAME_RE.fullmatch(name) is None
        or any(
            (byte < 0x20 and byte != 0x09) or byte == 0x7F
            for byte in value
        )
    ):
        raise ProxyPolicyError("invalid_request_header")
    return name.lower(), value


def _is_forwarding_identity_header(name: bytes) -> bool:
    return (
        name in {b"forwarded", b"x-real-ip", b"via"}
        or name.startswith(b"x-forwarded")
    )


def _request_destination(
    request: bytes,
) -> tuple[str, str, int, bytes]:
    header, body = request.split(b"\r\n\r\n", 1)
    lines = header.split(b"\r\n")
    try:
        method_raw, target_raw, version_raw = lines[0].split(b" ", 2)
        method = method_raw.decode("ascii").upper()
        target = target_raw.decode("ascii")
        version = version_raw.decode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise ProxyPolicyError("invalid_request_line") from exc
    if (
        len(target_raw) > MAX_HTTP_REQUEST_TARGET_BYTES
        or len(lines) - 1 > MAX_HTTP_REQUEST_HEADER_FIELDS
    ):
        raise ProxyPolicyError("http_request_metadata_too_large")
    if _HTTP_METHOD_RE.fullmatch(method_raw) is None:
        raise ProxyPolicyError("invalid_request_method")
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise ProxyPolicyError("unsupported_http_version")
    if method == "CONNECT":
        host, port = _parse_connect_target(target)
        return "https", host, port, b""

    try:
        parsed = urlsplit(target)
        parsed_host = parsed.hostname
        parsed_port = parsed.port
    except ValueError as exc:
        raise ProxyPolicyError("absolute_http_url_required") from exc
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or not parsed_host
        or parsed.fragment
    ):
        raise ProxyPolicyError("absolute_http_url_required")
    port = parsed_port if parsed_port is not None else 80
    origin = _normalized_origin("http", parsed_host, port)
    origin_target = parsed.path or "/"
    if parsed.query:
        origin_target += "?" + parsed.query
    try:
        first_line = b" ".join(
            (
                method_raw,
                origin_target.encode("ascii"),
                version_raw,
            )
        )
    except UnicodeError as exc:
        raise ProxyPolicyError("absolute_http_url_required") from exc
    filtered: list[bytes] = []
    host_seen = False
    for line in lines[1:]:
        name, value = _validated_header_parts(line)
        if name in {
            b"proxy-authorization",
            b"proxy-connection",
        } or _is_forwarding_identity_header(name):
            continue
        if name == b"host":
            if host_seen:
                raise ProxyPolicyError("duplicate_http_host_header")
            host_seen = True
            if _parse_http_host_authority(
                value,
                scheme="http",
            ) != origin:
                raise ProxyPolicyError("http_host_target_mismatch")
            # Send the upstream one unambiguous canonical authority even when
            # the equivalent client spelling used case, a trailing dot, or an
            # explicit default port.
            filtered.append(
                b"Host: " + _canonical_http_host_header(origin)
            )
            continue
        if name == b"connection":
            continue
        if name == b"upgrade":
            raise ProxyPolicyError("http_upgrade_not_supported")
        if name == b"expect" and value.strip(b" \t"):
            raise ProxyPolicyError("http_expectation_not_supported")
        filtered.append(line)
    if not host_seen:
        raise ProxyPolicyError("http_host_header_required")
    # This proxy deliberately handles one fully framed HTTP request per
    # connection. That prevents a second origin-form request from reusing the
    # first request's pinned IP and selecting another virtual host.
    filtered.append(b"Connection: close")
    forwarded = b"\r\n".join([first_line, *filtered]) + b"\r\n\r\n" + body
    return "http", origin.host, origin.port, forwarded


def _tunneled_origin_request(
    request: bytes,
    expected_origin: Origin,
) -> bytes:
    """Validate one origin-form request carried by an exact CONNECT grant.

    HTTPS is decrypted by the interception CA. Node's built-in environment
    proxy also uses CONNECT for some plain HTTP fetches, so an explicitly
    signed HTTP origin may carry one inspected plaintext request. Absolute
    form, nested CONNECT, HTTP/2, upgrades, ambiguous framing, and a different
    Host authority all fail before an upstream connection is opened.
    """

    if expected_origin.scheme not in {"http", "https"}:
        raise ProxyPolicyError("invalid_tunneled_origin")
    header, body = request.split(b"\r\n\r\n", 1)
    lines = header.split(b"\r\n")
    try:
        method_raw, target_raw, version_raw = lines[0].split(b" ", 2)
        method = method_raw.decode("ascii").upper()
        target = target_raw.decode("ascii")
        version = version_raw.decode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise ProxyPolicyError("invalid_request_line") from exc
    if (
        len(target_raw) > MAX_HTTP_REQUEST_TARGET_BYTES
        or len(lines) - 1 > MAX_HTTP_REQUEST_HEADER_FIELDS
    ):
        raise ProxyPolicyError("http_request_metadata_too_large")
    if _HTTP_METHOD_RE.fullmatch(method_raw) is None:
        raise ProxyPolicyError("invalid_request_method")
    if method == "CONNECT":
        raise ProxyPolicyError("nested_connect_not_allowed")
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise ProxyPolicyError("unsupported_http_version")
    if (
        not target
        or (
            target != "*"
            and (
                not target.startswith("/")
                or target.startswith("//")
            )
        )
        or (target == "*" and method != "OPTIONS")
        or "#" in target
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in target)
    ):
        raise ProxyPolicyError("origin_form_https_target_required")

    filtered: list[bytes] = []
    host_seen = False
    for line in lines[1:]:
        name, value = _validated_header_parts(line)
        if name in {
            b"proxy-authorization",
            b"proxy-connection",
        } or _is_forwarding_identity_header(name):
            continue
        if name == b"host":
            if host_seen:
                raise ProxyPolicyError("duplicate_http_host_header")
            host_seen = True
            if _parse_http_host_authority(
                value,
                scheme=expected_origin.scheme,
            ) != expected_origin:
                raise ProxyPolicyError(
                    (
                        "https_host_origin_mismatch"
                        if expected_origin.scheme == "https"
                        else "http_host_origin_mismatch"
                    )
                )
            filtered.append(
                b"Host: "
                + _canonical_http_host_header(expected_origin)
            )
            continue
        if name == b"connection":
            continue
        if name == b"upgrade":
            raise ProxyPolicyError("http_upgrade_not_supported")
        if name == b"expect" and value.strip(b" \t"):
            raise ProxyPolicyError("http_expectation_not_supported")
        filtered.append(line)
    if not host_seen:
        raise ProxyPolicyError("http_host_header_required")
    filtered.append(b"Connection: close")
    return (
        b"\r\n".join([
            b" ".join((method_raw, target_raw, version_raw)),
            *filtered,
        ])
        + b"\r\n\r\n"
        + body
    )


def _tunneled_https_request(
    request: bytes,
    expected_origin: Origin,
) -> bytes:
    """Compatibility wrapper for the HTTPS-specific validation entrypoint."""

    if expected_origin.scheme != "https":
        raise ProxyPolicyError("invalid_https_origin")
    return _tunneled_origin_request(request, expected_origin)


@dataclass(frozen=True, slots=True)
class _RequestBodyFraming:
    header: bytes
    initial_body: bytes
    content_length: int


def _normalized_outbound_wire_bytes(
    framing: _RequestBodyFraming,
) -> int:
    return len(framing.header) + 4 + framing.content_length


def _validated_request_body_framing(
    forwarded: bytes,
) -> _RequestBodyFraming:
    header, body = forwarded.split(b"\r\n\r\n", 1)
    lines = header.split(b"\r\n")
    if len(lines) - 1 > MAX_HTTP_REQUEST_HEADER_FIELDS:
        raise ProxyPolicyError("http_request_metadata_too_large")
    request_line = lines[0]
    try:
        method = request_line.split(b" ", 1)[0].decode("ascii").upper()
    except UnicodeError as exc:
        raise ProxyPolicyError("invalid_request_method") from exc
    content_lengths: list[bytes] = []
    transfer_encodings: list[bytes] = []
    for line in lines[1:]:
        name, value = _validated_header_parts(line)
        if name == b"content-length":
            content_lengths.append(value.strip(b" \t"))
        elif name == b"transfer-encoding":
            transfer_encodings.append(value.strip(b" \t").lower())
    if len(content_lengths) > 1 or len(transfer_encodings) > 1:
        raise ProxyPolicyError("ambiguous_http_request_framing")
    if content_lengths and transfer_encodings:
        raise ProxyPolicyError("ambiguous_http_request_framing")
    if transfer_encodings:
        # Forwarding chunk extensions/trailers to a second HTTP parser creates
        # an avoidable parser-differential boundary. A streaming dechunker
        # cannot emit the one canonical Content-Length before seeing the
        # entire body without reintroducing the multi-megabyte buffer this
        # proxy is designed to avoid, so fail closed.
        raise ProxyPolicyError(
            "unsupported_http_transfer_encoding"
        )
    if content_lengths:
        raw_length = content_lengths[0]
        if (
            not raw_length
            or re.fullmatch(rb"[0-9]+", raw_length) is None
        ):
            raise ProxyPolicyError("invalid_http_content_length")
        expected = int(raw_length)
        if expected > MAX_HTTP_REQUEST_BODY_BYTES:
            raise ProxyPolicyError("http_request_body_too_large")
        if len(body) > expected:
            raise ProxyPolicyError(
                "http_request_pipelining_not_allowed"
            )
    else:
        if body:
            raise ProxyPolicyError(
                "http_request_pipelining_not_allowed"
            )
        expected = 0
    # Retrieval methods may transmit only their authorized target and bounded
    # headers. A request body on GET/HEAD is non-portable and creates an
    # unnecessary upload/exfiltration channel, so it is rejected even when an
    # exact URL rule otherwise matches.
    if method in {"GET", "HEAD"} and expected:
        raise ProxyPolicyError(
            "read_only_http_method_body_not_allowed"
        )
    return _RequestBodyFraming(
        header=header,
        initial_body=body,
        content_length=expected,
    )


def _send_request_part(
    upstream: socket.socket,
    content: bytes,
    *,
    absolute_deadline: float,
) -> None:
    if not content:
        return
    remaining = absolute_deadline - time.monotonic()
    if remaining <= 0:
        raise ProxyTransportTimeoutError(
            "upstream_request_write_timeout"
        )
    upstream.settimeout(
        min(remaining, UPSTREAM_REQUEST_WRITE_TIMEOUT_SECONDS)
    )
    try:
        upstream.sendall(content)
    except (socket.timeout, TimeoutError) as exc:
        raise ProxyTransportTimeoutError(
            "upstream_request_write_timeout"
        ) from exc


def _transport_error_status(error: ProxyTransportError) -> int:
    """Map stable upstream failure classes without inspecting error text."""

    return (
        504
        if isinstance(error, ProxyTransportTimeoutError)
        else 502
    )


def _forward_single_http_request(
    connection: socket.socket,
    upstream: socket.socket,
    forwarded: bytes,
    *,
    framing: _RequestBodyFraming | None = None,
) -> None:
    """Stream one validated Content-Length request with constant memory."""

    body_framing = (
        _validated_request_body_framing(forwarded)
        if framing is None
        else framing
    )
    initial_write_deadline = (
        time.monotonic() + UPSTREAM_REQUEST_WRITE_TIMEOUT_SECONDS
    )
    _send_request_part(
        upstream,
        body_framing.header + b"\r\n\r\n",
        absolute_deadline=initial_write_deadline,
    )
    initial = body_framing.initial_body
    for offset in range(0, len(initial), COPY_CHUNK_BYTES):
        _send_request_part(
            upstream,
            initial[offset : offset + COPY_CHUNK_BYTES],
            absolute_deadline=initial_write_deadline,
        )

    remaining = body_framing.content_length - len(initial)
    read_deadline = _ReadDeadline.start(
        absolute_timeout_seconds=(
            HTTP_BODY_ABSOLUTE_READ_TIMEOUT_SECONDS
        ),
        idle_timeout_seconds=HTTP_BODY_IDLE_TIMEOUT_SECONDS,
    )
    # A legal streaming upload may consume most of its bounded body-read
    # window. Do not reuse the header's shorter write deadline for later
    # chunks; retain one absolute transaction bound while allowing the final
    # chunk its own bounded upstream write window.
    body_write_deadline = (
        read_deadline.absolute_deadline
        + UPSTREAM_REQUEST_WRITE_TIMEOUT_SECONDS
    )
    while remaining:
        chunk = read_deadline.recv(
            connection,
            min(COPY_CHUNK_BYTES, remaining),
            error_code="incomplete_http_request_body",
        )
        if not chunk:
            raise ProxyPolicyError("incomplete_http_request_body")
        _send_request_part(
            upstream,
            chunk,
            absolute_deadline=body_write_deadline,
        )
        remaining -= len(chunk)


def _connect_pinned(destination: Destination) -> socket.socket:
    connection = socket.socket(destination.family, socket.SOCK_STREAM)
    connection.settimeout(CONNECT_TIMEOUT_SECONDS)
    sockaddr: tuple[object, ...]
    if destination.family == socket.AF_INET6:
        sockaddr = (destination.address, destination.port, 0, 0)
    else:
        sockaddr = (destination.address, destination.port)
    try:
        connection.connect(sockaddr)
    except Exception:
        connection.close()
        raise
    return connection


def _relay(left: socket.socket, right: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    started = last_activity = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - started >= MAX_TUNNEL_SECONDS:
                return
            idle_remaining = IDLE_TIMEOUT_SECONDS - (now - last_activity)
            if idle_remaining <= 0:
                return
            events = selector.select(min(idle_remaining, 1.0))
            if not events:
                continue
            for key, _mask in events:
                source = key.fileobj
                destination = key.data
                try:
                    chunk = source.recv(COPY_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    return
                if len(chunk) > MAX_BUFFER_BYTES:
                    return
                view = memoryview(chunk)
                while view:
                    try:
                        sent = destination.send(view)
                    except BlockingIOError:
                        time.sleep(0.001)
                        continue
                    view = view[sent:]
                last_activity = time.monotonic()
    finally:
        selector.close()


def _relay_response_only(
    client: socket.socket,
    upstream: socket.socket,
    budget: PolicyBudgetReservation | None = None,
) -> None:
    """Relay one HTTP response without accepting a second client request."""

    started = last_activity = time.monotonic()
    while True:
        now = time.monotonic()
        if now - started >= MAX_TUNNEL_SECONDS:
            return
        idle_remaining = IDLE_TIMEOUT_SECONDS - (now - last_activity)
        if idle_remaining <= 0:
            return
        upstream.settimeout(min(idle_remaining, 1.0))
        try:
            chunk = upstream.recv(COPY_CHUNK_BYTES)
        except (socket.timeout, TimeoutError):
            continue
        except (ConnectionError, OSError):
            return
        if not chunk:
            return
        if budget is not None:
            try:
                budget.consume_response_wire(len(chunk))
            except ProxyPolicyError:
                # A local error response cannot be appended safely after an
                # upstream response has begun. Closing both sides is the
                # unambiguous fail-closed framing boundary.
                return
        client.settimeout(min(idle_remaining, 1.0))
        try:
            client.sendall(chunk)
        except (socket.timeout, ConnectionError, OSError):
            return
        last_activity = time.monotonic()


def _safe_error(connection: socket.socket, status: int, reason: str) -> None:
    body = (reason[:120] + "\n").encode("ascii", errors="replace")
    response = (
        f"HTTP/1.1 {status} {reason[:60]}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "Content-Type: text/plain\r\n\r\n"
    ).encode("ascii", errors="replace") + body
    try:
        connection.sendall(response)
    except OSError:
        pass


def _safe_error_then_drain(
    connection: socket.socket,
    status: int,
    reason: str,
) -> None:
    """Deliver one typed pre-tunnel error without a close/reset race.

    A bridge may have queued the browser's request just after this proxy
    rejected the authenticated preface. Closing an AF_UNIX stream with those
    bytes unread can reset the bridge side and discard the HTTP error that was
    already sent. Half-close the response direction first, then discard a
    strictly bounded amount of input while the bridge relays the response and
    closes its write half. No request bytes are interpreted or buffered here.
    """

    _safe_error(connection, status, reason)
    try:
        connection.shutdown(socket.SHUT_WR)
    except OSError:
        return
    deadline = (
        time.monotonic()
        + ERROR_INPUT_DRAIN_ABSOLUTE_TIMEOUT_SECONDS
    )
    remaining = MAX_ERROR_INPUT_DRAIN_BYTES
    while remaining > 0:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            return
        try:
            connection.settimeout(timeout)
            chunk = connection.recv(min(COPY_CHUNK_BYTES, remaining))
        except (socket.timeout, TimeoutError, ConnectionError, OSError):
            return
        if not chunk:
            return
        remaining -= len(chunk)


class ProxyHandler(socketserver.BaseRequestHandler):
    policy = AddressPolicy()
    certificate_authority: CertificateAuthority | None = None
    upstream_tls_policy = UpstreamTlsPolicy()
    trust_generation: str | None = None
    scope_ledger = PolicyScopeLedger()

    def origin_allowlist_for_request(
        self,
        signed_origins: Iterable[Origin | tuple[str, str, int]] = (),
    ) -> Iterable[Origin | tuple[str, str, int]]:
        """Return trusted exact origins for this one connection/request.

        The signed preface is the sole authority issuer. Request headers are
        intentionally not consulted, and an empty signed set remains
        deny-all.
        """

        return signed_origins

    def handle(self) -> None:
        upstream: socket.socket | None = None
        tunnel_established = False
        client_tls: ssl.SSLSocket | None = None
        budget: PolicyBudgetReservation | None = None
        try:
            trust_generation = self.trust_generation
            if (
                not isinstance(trust_generation, str)
                or _SHA256_HEX_RE.fullmatch(trust_generation) is None
            ):
                raise ProxyPolicyError(
                    "policy_trust_generation_unavailable"
                )
            authority_at_admission = self.certificate_authority
            if (
                authority_at_admission is not None
                and not hmac.compare_digest(
                    authority_at_admission.generation_id,
                    trust_generation,
                )
            ):
                raise ProxyPolicyError(
                    "interception_trust_generation_mismatch"
                )
            signed_policy, buffered = _read_policy_preface(
                self.request,
                expected_trust_generation=trust_generation,
            )
            budget = self.scope_ledger.admit(signed_policy)
            request = _read_headers(self.request, initial=buffered)
            scheme, host, port, forwarded = _request_destination(request)
            trusted_origins = normalize_origin_allowlist(
                self.origin_allowlist_for_request(
                    signed_policy.origins
                )
            )
            if not forwarded:
                # CONNECT has no scheme on the wire. Prefer the one and only
                # exact signed origin for this authority. This permits Node's
                # native HTTP fetch proxy mode without ever inferring scheme
                # from model-controlled headers or widening authority.
                canonical_host = _normalized_host(host)
                matching_connect_origins = {
                    origin
                    for origin in trusted_origins
                    if (
                        origin.host == canonical_host
                        and origin.port == port
                    )
                }
                if len(matching_connect_origins) > 1:
                    raise ProxyPolicyError(
                        "ambiguous_connect_origin_scheme"
                    )
                if matching_connect_origins:
                    scheme = next(
                        iter(matching_connect_origins)
                    ).scheme
            requested_origin = _normalized_origin(
                scheme,
                host,
                port,
            )
            framing: _RequestBodyFraming | None = None
            if forwarded:
                _authorize_exact_request(
                    signed_policy,
                    requested_origin,
                    forwarded,
                )
            if forwarded and signed_policy.version == 3:
                framing = _validated_request_body_framing(
                    forwarded
                )
                if budget is not None:
                    budget.consume_outbound(
                        _normalized_outbound_wire_bytes(framing)
                    )
            destination: Destination | None = None
            # Version 3 delays all DNS and upstream connection activity until
            # the inspected inner CONNECT request has consumed its signed
            # outbound budget. Version 2 retains its historical ordering.
            if forwarded or signed_policy.version == 2:
                destination = self.policy.resolve(
                    scheme,
                    host,
                    port,
                    origin_allowlist=trusted_origins,
                    signed_private_origins=(
                        signed_policy.private_origins
                    ),
                )
            if forwarded:
                assert destination is not None
                if framing is None:
                    framing = _validated_request_body_framing(
                        forwarded
                    )
                assert framing is not None
                try:
                    upstream = _connect_pinned(destination)
                    _forward_single_http_request(
                        self.request,
                        upstream,
                        forwarded,
                        framing=framing,
                    )
                except ProxyTransportError as exc:
                    _safe_error_then_drain(
                        self.request,
                        _transport_error_status(exc),
                        str(exc),
                    )
                    return
                except ProxyPolicyError as exc:
                    _safe_error_then_drain(
                        self.request,
                        (
                            408
                            if str(exc)
                            == "incomplete_http_request_body"
                            else 400
                        ),
                        str(exc),
                    )
                    return
                except (
                    ConnectionError,
                    OSError,
                    TimeoutError,
                ):
                    _safe_error_then_drain(
                        self.request,
                        502,
                        "destination_connection_failed",
                    )
                    return
                try:
                    upstream.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                _relay_response_only(self.request, upstream, budget)
                return
            if requested_origin.scheme == "http":
                self.request.sendall(
                    b"HTTP/1.1 200 Connection Established\r\n"
                    b"Proxy-Agent: chatds-skill-egress\r\n\r\n"
                )
                tunnel_established = True
                try:
                    tunneled_request = _read_headers(self.request)
                    forwarded_http = _tunneled_origin_request(
                        tunneled_request,
                        requested_origin,
                    )
                    framing = _validated_request_body_framing(
                        forwarded_http
                    )
                    _authorize_exact_request(
                        signed_policy,
                        requested_origin,
                        forwarded_http,
                    )
                    if budget is not None:
                        budget.consume_outbound(
                            _normalized_outbound_wire_bytes(framing)
                        )
                    if destination is None:
                        destination = self.policy.resolve(
                            scheme,
                            host,
                            port,
                            origin_allowlist=trusted_origins,
                            signed_private_origins=(
                                signed_policy.private_origins
                            ),
                        )
                except ProxyPolicyError as exc:
                    _safe_error(self.request, 403, str(exc))
                    return
                try:
                    assert destination is not None
                    upstream = _connect_pinned(destination)
                    _forward_single_http_request(
                        self.request,
                        upstream,
                        forwarded_http,
                        framing=framing,
                    )
                except ProxyTransportError as exc:
                    _safe_error_then_drain(
                        self.request,
                        _transport_error_status(exc),
                        str(exc),
                    )
                    return
                except ProxyPolicyError as exc:
                    _safe_error_then_drain(
                        self.request,
                        (
                            408
                            if str(exc)
                            == "incomplete_http_request_body"
                            else 400
                        ),
                        str(exc),
                    )
                    return
                except (
                    ConnectionError,
                    OSError,
                    TimeoutError,
                ):
                    _safe_error_then_drain(
                        self.request,
                        502,
                        "destination_connection_failed",
                    )
                    return
                try:
                    upstream.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                _relay_response_only(self.request, upstream, budget)
                return
            authority = self.certificate_authority
            if authority is None:
                raise ProxyPolicyError(
                    "interception_certificate_authority_unavailable"
                )
            if destination is not None:
                self.upstream_tls_policy.authorize(destination)
            self.request.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: chatds-skill-egress\r\n\r\n"
            )
            tunnel_established = True
            try:
                client_context = authority.server_context(
                    requested_origin.host
                )
                self.request.settimeout(
                    TLS_HANDSHAKE_TIMEOUT_SECONDS
                )
                client_tls = client_context.wrap_socket(
                    self.request,
                    server_side=True,
                )
                if client_tls.selected_alpn_protocol() not in {
                    None,
                    "http/1.1",
                }:
                    raise ProxyPolicyError(
                        "client_http2_not_allowed"
                    )
                encrypted_request = _read_headers(client_tls)
                exact_origin = requested_origin
                forwarded_https = _tunneled_https_request(
                    encrypted_request,
                    exact_origin,
                )
                framing = _validated_request_body_framing(
                    forwarded_https,
                )
                _authorize_exact_request(
                    signed_policy,
                    exact_origin,
                    forwarded_https,
                )
                if budget is not None:
                    budget.consume_outbound(
                        _normalized_outbound_wire_bytes(framing)
                    )
                if destination is None:
                    destination = self.policy.resolve(
                        scheme,
                        host,
                        port,
                        origin_allowlist=trusted_origins,
                        signed_private_origins=(
                            signed_policy.private_origins
                        ),
                    )
                    self.upstream_tls_policy.authorize(
                        destination
                    )
            except ProxyPolicyError as exc:
                if client_tls is not None:
                    _safe_error(client_tls, 403, str(exc))
                return
            except (ConnectionError, OSError, ssl.SSLError, TimeoutError):
                return

            try:
                assert destination is not None
                raw_upstream = _connect_pinned(destination)
                try:
                    upstream = self.upstream_tls_policy.wrap(
                        raw_upstream,
                        destination,
                    )
                except BaseException:
                    raw_upstream.close()
                    raise
                _forward_single_http_request(
                    client_tls,
                    upstream,
                    forwarded_https,
                    framing=framing,
                )
                # Do not call the raw socket shutdown API on SSLSocket: doing so
                # bypasses TLS close semantics and can expose encrypted records
                # through subsequent recv calls. Connection: close and the
                # one-request response-only relay provide the framing boundary.
                _relay_response_only(client_tls, upstream, budget)
            except ProxyTransportError as exc:
                _safe_error(
                    client_tls,
                    _transport_error_status(exc),
                    str(exc),
                )
            except ProxyPolicyError as exc:
                _safe_error(
                    client_tls,
                    (
                        408
                        if str(exc) == "incomplete_http_request_body"
                        else 502
                    ),
                    str(exc),
                )
            except (ConnectionError, OSError, ssl.SSLError, TimeoutError):
                _safe_error(
                    client_tls,
                    502,
                    "destination_connection_failed",
                )
            return
        except ProxyTransportError as exc:
            if not tunnel_established:
                _safe_error_then_drain(
                    self.request,
                    _transport_error_status(exc),
                    str(exc),
                )
        except ProxyPolicyError as exc:
            if not tunnel_established:
                _safe_error_then_drain(
                    self.request,
                    403,
                    str(exc),
                )
        except (ConnectionError, OSError, TimeoutError):
            if not tunnel_established:
                _safe_error_then_drain(
                    self.request,
                    502,
                    "destination_connection_failed",
                )
        finally:
            if budget is not None:
                budget.release()
            if upstream is not None:
                upstream.close()
            if client_tls is not None:
                try:
                    client_tls.close()
                except OSError:
                    pass


class _BoundedThreadingServer:
    """Shared bounded-admission behavior for TCP and Unix listeners."""

    def __init__(self, *args, **kwargs):
        if not (
            1
            <= MAX_CONCURRENT_CONNECTIONS
            <= MAX_GLOBAL_CONNECTION_LIMIT
        ):
            raise ProxyPolicyError("invalid_connection_limit")
        self._connection_slots = threading.BoundedSemaphore(
            MAX_CONCURRENT_CONNECTIONS
        )
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            _safe_error(request, 503, "proxy_connection_limit_reached")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class ThreadingProxyServer(
    _BoundedThreadingServer,
    socketserver.ThreadingTCPServer,
):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64


class ThreadingUnixProxyServer(
    _BoundedThreadingServer,
    socketserver.ThreadingUnixStreamServer,
):
    daemon_threads = True
    request_queue_size = 64


def _prepare_unix_socket_path(value: str) -> Path:
    path = Path(value)
    if not value or not path.is_absolute() or "\x00" in value:
        raise ProxyPolicyError("invalid_listen_socket")
    parent = path.parent
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise ProxyPolicyError("listen_socket_parent_unavailable") from exc
    if (
        stat.S_ISLNK(parent_stat.st_mode)
        or not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or parent_stat.st_mode & stat.S_IWOTH
    ):
        raise ProxyPolicyError("unsafe_listen_socket_parent")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise ProxyPolicyError("listen_socket_unavailable") from exc
    if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
        raise ProxyPolicyError("unsafe_existing_listen_socket")
    try:
        path.unlink()
    except OSError as exc:
        raise ProxyPolicyError("listen_socket_unavailable") from exc
    return path


class _UnixProxyContext:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self.server: ThreadingUnixProxyServer | None = None

    def __enter__(self) -> ThreadingUnixProxyServer:
        server = ThreadingUnixProxyServer(str(self.socket_path), ProxyHandler)
        try:
            os.chmod(self.socket_path, 0o660)
        except OSError:
            server.server_close()
            raise
        self.server = server
        return server

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.server is not None:
            self.server.server_close()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


def healthcheck(
    host: str = "127.0.0.1",
    port: int = LISTEN_PORT,
    *,
    socket_path: str = LISTEN_SOCKET,
) -> bool:
    try:
        if socket_path:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(2)
            connection.connect(socket_path)
        else:
            connection = socket.create_connection((host, port), timeout=2)
        with connection:
            connection.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
            response = connection.recv(256)
        return response.startswith(b"HTTP/1.1 403 ")
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        return 0 if healthcheck() else 1
    authority: CertificateAuthority | None = None
    try:
        authority = CertificateAuthority()
        ProxyHandler.certificate_authority = authority
        ProxyHandler.trust_generation = authority.generation_id
        ProxyHandler.upstream_tls_policy = UpstreamTlsPolicy()
        listener = (
            _UnixProxyContext(_prepare_unix_socket_path(LISTEN_SOCKET))
            if LISTEN_SOCKET
            else ThreadingProxyServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
        )
        with listener as server:
            server.serve_forever(poll_interval=0.25)
    except Exception as exc:
        print(
            f"skill egress proxy failed: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        ProxyHandler.certificate_authority = None
        ProxyHandler.trust_generation = None
        if authority is not None:
            authority.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
