"""Controller-owned loopback bridge to the fixed Skill egress policy proxy.

The untrusted session-sandbox worker has no network interface beyond loopback. It can
only reach this fixed TCP listener, which relays bytes to the policy proxy's
fixed Unix-domain socket. The controller signs exact HTTP-method/URL-prefix
rules and, when deployment enables it, one fixed public GET/HEAD profile into
every proxy connection; the bridge has no authority input controlled by the
Skill process.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import secrets
import socket
import socketserver
import ssl
import stat
import struct
import sys
import threading
import time
from typing import Final
from urllib.parse import urlsplit, urlunsplit


LISTEN_HOST: Final[str] = "127.0.0.1"
LISTEN_PORT: Final[int] = 18080
# Bump this whenever the signed rule schema or its matching semantics change.
# Container builds assert the value so a stale sandbox base cannot silently
# disagree with the Supervisor policy compiler.
EGRESS_POLICY_RUNTIME_VERSION: Final[str] = "signed-route-idle-v2"
PROXY_SOCKET_PATH: Final[Path] = Path(
    "/run/chatds-skill-egress/proxy.sock"
)
PROXY_CA_CERTIFICATE_PATH: Final[Path] = Path(
    "/run/chatds-skill-egress/ca.pem"
)
PROXY_LEAF_SPKI_PATH: Final[Path] = Path(
    "/run/chatds-skill-egress/leaf.spki"
)
PROXY_TRUST_GENERATION_PATH: Final[Path] = Path(
    "/run/chatds-skill-egress/generation.json"
)
EXPECTED_PROXY_UID: Final[int] = 65531
EXPECTED_BRIDGE_GID: Final[int] = 65530
MAX_CONNECTIONS: Final[int] = 8
MAX_DIRECTION_BUFFER_BYTES: Final[int] = 1024 * 1024
IDLE_TIMEOUT_SECONDS: Final[float] = 14_400.0
CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0
HANDLER_DRAIN_TIMEOUT_SECONDS: Final[float] = 10.0
POLICY_PREFACE_PREFIX: Final[bytes] = b"CHATDS-EGRESS-POLICY-V1 "
MAX_POLICY_PREFACE_BYTES: Final[int] = 64 * 1024
POLICY_TTL_SECONDS: Final[int] = 60
POLICY_KEY_DERIVATION_LABEL: Final[bytes] = (
    b"chatds-skill-egress-policy-hmac-v1"
)
MAX_POLICY_ORIGINS: Final[int] = 128
MAX_POLICY_RULES: Final[int] = 256
MAX_SIGNED_RESPONSE_IDLE_TIMEOUT_SECONDS: Final[int] = 14_400
BOUNDED_EXCHANGE_PROFILE: Final[str] = "bounded_controlled_exchange"
AUDIT_RECEIPT_VERSION: Final[int] = 1
DEFAULT_MAX_OUTBOUND_BYTES: Final[int] = 16 * 1024 * 1024
DEFAULT_MAX_REQUESTS: Final[int] = 2_048
DEFAULT_MAX_RESPONSE_WIRE_BYTES: Final[int] = 512 * 1024 * 1024
ABSOLUTE_MAX_OUTBOUND_BYTES: Final[int] = 1024 * 1024 * 1024
ABSOLUTE_MAX_REQUESTS: Final[int] = 65_536
ABSOLUTE_MAX_RESPONSE_WIRE_BYTES: Final[int] = (
    16 * 1024 * 1024 * 1024
)
_LIMIT_KEYS: Final[frozenset[str]] = frozenset({
    "max_outbound_bytes",
    "max_requests",
    "max_response_wire_bytes",
})
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
_INVALID_EGRESS_PERCENT_ESCAPE: Final[re.Pattern[str]] = re.compile(
    r"%(?![0-9A-F]{2})"
)
_INVALID_EGRESS_ENCODED_PATH: Final[re.Pattern[str]] = re.compile(
    r"%(?:2e|2f|5c|25|23|3f|0[0-9a-f]|1[0-9a-f]|7f)",
    re.IGNORECASE,
)
_SO_PEERCRED_SIZE: Final[int] = struct.calcsize("3i")
_SPKI_SHA256_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9+/]{43}=$"
)
MAX_CA_CERTIFICATE_BYTES: Final[int] = 64 * 1024
MAX_SPKI_FILE_BYTES: Final[int] = 256
MAX_TRUST_GENERATION_MANIFEST_BYTES: Final[int] = 4 * 1024
TRUST_GENERATION_MANIFEST_VERSION: Final[int] = 1
_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class BridgeConfigurationError(RuntimeError):
    """The deployment-owned proxy socket boundary is not trustworthy."""


def _canonical_json_bytes(value: object) -> bytes:
    """Encode one deterministic, bounded-control-plane JSON value."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _validated_v3_limits(
    value: dict[str, object] | None,
) -> dict[str, int]:
    """Return exact, locally bounded per-call exchange limits."""

    if value is None:
        return {
            "max_outbound_bytes": DEFAULT_MAX_OUTBOUND_BYTES,
            "max_requests": DEFAULT_MAX_REQUESTS,
            "max_response_wire_bytes": (
                DEFAULT_MAX_RESPONSE_WIRE_BYTES
            ),
        }
    if not isinstance(value, dict) or set(value) != _LIMIT_KEYS:
        raise BridgeConfigurationError(
            "invalid bounded exchange limits"
        )
    hard_limits = {
        "max_outbound_bytes": ABSOLUTE_MAX_OUTBOUND_BYTES,
        "max_requests": ABSOLUTE_MAX_REQUESTS,
        "max_response_wire_bytes": (
            ABSOLUTE_MAX_RESPONSE_WIRE_BYTES
        ),
    }
    normalized: dict[str, int] = {}
    for key in (
        "max_outbound_bytes",
        "max_requests",
        "max_response_wire_bytes",
    ):
        raw = value.get(key)
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int)
            or raw < 1
            or raw > hard_limits[key]
        ):
            raise BridgeConfigurationError(
                "invalid bounded exchange limits"
            )
        normalized[key] = raw
    return normalized


def _policy_auth_key(value: str | None = None) -> bytes:
    token = (
        os.environ.get("SKILL_EGRESS_POLICY_TOKEN", "")
        if value is None
        else value
    )
    try:
        encoded = token.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise BridgeConfigurationError(
            "egress policy authentication is unavailable"
        ) from exc
    if not 32 <= len(encoded) <= 4_096:
        raise BridgeConfigurationError(
            "egress policy authentication is unavailable"
        )
    return hmac.new(
        encoded,
        POLICY_KEY_DERIVATION_LABEL,
        hashlib.sha256,
    ).digest()


def _canonical_origin(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8_192:
        raise BridgeConfigurationError("invalid egress origin policy")
    try:
        parsed = urlsplit(value)
        parsed_host = parsed.hostname
        parsed_port = parsed.port
        port = (
            parsed_port
            if parsed_port is not None
            else (
                443 if parsed.scheme.casefold() == "https" else 80
            )
        )
    except ValueError as exc:
        raise BridgeConfigurationError(
            "invalid egress origin policy"
        ) from exc
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or not parsed_host
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BridgeConfigurationError("invalid egress origin policy")
    raw_host = parsed_host.rstrip(".").casefold()
    if (
        not raw_host
        or "%" in raw_host
        or any(char in raw_host for char in "*?[]")
        or any(
            ord(char) < 0x20 or ord(char) == 0x7F
            for char in raw_host
        )
    ):
        raise BridgeConfigurationError("invalid egress origin policy")
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise BridgeConfigurationError(
                "invalid egress origin policy"
            ) from exc
        if (
            len(host) > 253
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(
                    char
                    not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                    for char in label
                )
                for label in host.split(".")
            )
        ):
            raise BridgeConfigurationError(
                "invalid egress origin policy"
            )
    else:
        host = (
            f"[{address.compressed}]"
            if address.version == 6
            else address.compressed
        )
    canonical = f"{scheme}://{host}:{port}"
    if canonical != value:
        raise BridgeConfigurationError(
            "egress origins must be canonical"
        )
    return canonical


def _canonical_url_prefix(value: object) -> str:
    """Validate the executor's exact URL-prefix wire representation."""

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
        raise BridgeConfigurationError("invalid exact egress rule")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise BridgeConfigurationError(
            "invalid exact egress rule"
        ) from exc
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise BridgeConfigurationError("invalid exact egress rule")
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
    origin = _canonical_origin(
        f"{scheme}://{rendered_host}:{port}"
    )
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
        raise BridgeConfigurationError("invalid exact egress rule")
    query = parsed.query
    if (
        "{" in query
        or "}" in query
        or ";" in query
        or _INVALID_EGRESS_PERCENT_ESCAPE.search(query)
        or re.search(
            r"%(?:0[0-9A-F]|1[0-9A-F]|7F)",
            query,
            re.IGNORECASE,
        )
    ):
        raise BridgeConfigurationError("invalid exact egress rule")
    canonical = urlunsplit((
        scheme,
        urlsplit(origin).netloc,
        path,
        query,
        "",
    ))
    if canonical != value:
        raise BridgeConfigurationError(
            "exact egress URL prefixes must be canonical"
        )
    return canonical


def _validated_exact_policy(
    origins: tuple[str, ...],
    rules: tuple[dict[str, object], ...],
    private_origins: tuple[str, ...],
) -> tuple[
    tuple[str, ...],
    tuple[dict[str, object], ...],
    tuple[str, ...],
]:
    """Independently validate the signed per-execution policy projection."""

    if (
        not isinstance(origins, tuple)
        or len(origins) > MAX_POLICY_ORIGINS
        or not isinstance(rules, tuple)
        or len(rules) > MAX_POLICY_RULES
        or not isinstance(private_origins, tuple)
        or len(private_origins) > MAX_POLICY_ORIGINS
    ):
        raise BridgeConfigurationError("invalid exact egress policy")
    canonical_origins = tuple(
        _canonical_origin(value) for value in origins
    )
    canonical_private = tuple(
        _canonical_origin(value) for value in private_origins
    )
    if (
        len(set(canonical_origins)) != len(canonical_origins)
        or len(set(canonical_private)) != len(canonical_private)
    ):
        raise BridgeConfigurationError(
            "exact egress origins must be unique"
        )

    canonical_rules: list[dict[str, object]] = []
    derived_origins: list[str] = []
    seen_rules: set[tuple[str, tuple[str, ...], bool]] = set()
    for raw_rule in rules:
        keys = set(raw_rule) if isinstance(raw_rule, dict) else set()
        if (
            not isinstance(raw_rule, dict)
            or not {"methods", "url_prefix"}.issubset(keys)
            or not keys.issubset({
                "methods",
                "url_prefix",
                "query_exact",
                "response_idle_timeout_seconds",
            })
            or not isinstance(raw_rule.get("methods"), list)
            or type(raw_rule.get("query_exact", False)) is not bool
        ):
            raise BridgeConfigurationError(
                "invalid exact egress rule"
            )
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
            raise BridgeConfigurationError(
                "invalid exact egress methods"
            )
        canonical_methods = tuple(
            method
            for method in EGRESS_METHOD_ORDER
            if method in set(methods_raw)
        )
        prefix = _canonical_url_prefix(raw_rule.get("url_prefix"))
        query_exact = bool(raw_rule.get("query_exact", False))
        response_idle_timeout = raw_rule.get(
            "response_idle_timeout_seconds"
        )
        if (
            response_idle_timeout is not None
            and (
                type(response_idle_timeout) is not int
                or not 1 <= response_idle_timeout
                <= MAX_SIGNED_RESPONSE_IDLE_TIMEOUT_SECONDS
                or canonical_methods != ("POST",)
                or not query_exact
            )
        ):
            raise BridgeConfigurationError(
                "invalid exact egress response idle timeout"
            )
        coordinate = (prefix, canonical_methods, query_exact)
        if (
            list(canonical_methods) != methods_raw
            or coordinate in seen_rules
        ):
            raise BridgeConfigurationError(
                "exact egress rules must be canonical and unique"
            )
        seen_rules.add(coordinate)
        origin = (
            f"{urlsplit(prefix).scheme}://"
            f"{urlsplit(prefix).netloc}"
        )
        if origin not in derived_origins:
            derived_origins.append(origin)
        canonical_rule: dict[str, object] = {
            "methods": list(canonical_methods),
            "url_prefix": prefix,
        }
        if query_exact:
            canonical_rule["query_exact"] = True
        if response_idle_timeout is not None:
            canonical_rule["response_idle_timeout_seconds"] = (
                response_idle_timeout
            )
        canonical_rules.append(canonical_rule)
    if tuple(derived_origins) != canonical_origins:
        raise BridgeConfigurationError(
            "egress origins must exactly project the URL rules"
        )
    if any(origin not in set(canonical_origins) for origin in canonical_private):
        raise BridgeConfigurationError(
            "private origins must be exact-rule origins"
        )
    return (
        canonical_origins,
        tuple(canonical_rules),
        canonical_private,
    )


def _validated_public_read_profile(
    value: object,
) -> dict[str, object] | None:
    """Validate the one deployment-owned public retrieval profile."""

    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"methods", "ports"}
        or value.get("methods") != ["GET", "HEAD"]
        or value.get("ports") != [80, 443]
    ):
        raise BridgeConfigurationError("invalid public-read egress profile")
    return {"methods": ["GET", "HEAD"], "ports": [80, 443]}


def _authority_projection_sha256(
    origins: tuple[str, ...],
    rules: tuple[dict[str, object], ...],
    private_origins: tuple[str, ...],
    public_read: dict[str, object] | None = None,
) -> str:
    """Bind every authority-bearing policy coordinate without exposing it."""

    return _canonical_json_sha256({
        "origins": list(origins),
        "egress_rules": list(rules),
        "private_origins": list(private_origins),
        "public_read": public_read,
    })


def _policy_preface(
    origins: tuple[str, ...],
    *,
    egress_rules: tuple[dict[str, object], ...],
    private_origins: tuple[str, ...],
    public_read: dict[str, object] | None = None,
    auth_key: bytes,
    trust_generation: str,
    budget_scope_sha256: str | None = None,
    call_id_sha256: str | None = None,
    limits: dict[str, object] | None = None,
) -> bytes:
    if _SHA256_HEX_RE.fullmatch(trust_generation) is None:
        raise BridgeConfigurationError(
            "proxy trust generation is unavailable"
        )
    v3_requested = any(
        value is not None
        for value in (
            budget_scope_sha256,
            call_id_sha256,
            limits,
        )
    )
    if (
        not v3_requested
        and any(
            "response_idle_timeout_seconds" in rule
            for rule in egress_rules
        )
    ):
        raise BridgeConfigurationError(
            "signed response idle timeout requires bounded policy v3"
        )
    normalized_limits: dict[str, int] | None = None
    if v3_requested:
        if (
            not isinstance(budget_scope_sha256, str)
            or _SHA256_HEX_RE.fullmatch(
                budget_scope_sha256
            ) is None
            or not isinstance(call_id_sha256, str)
            or _SHA256_HEX_RE.fullmatch(call_id_sha256) is None
        ):
            raise BridgeConfigurationError(
                "invalid bounded exchange identity"
            )
        normalized_limits = _validated_v3_limits(limits)
    unsigned = {
        "version": 3 if v3_requested else 2,
        "expires_unix": int(time.time()) + POLICY_TTL_SECONDS,
        "nonce": secrets.token_hex(16),
        "origins": list(origins),
        "egress_rules": list(egress_rules),
        "private_origins": list(private_origins),
        "trust_generation": trust_generation,
    }
    if v3_requested:
        assert normalized_limits is not None
        unsigned.update({
            "public_read": _validated_public_read_profile(public_read),
            "budget_scope_sha256": budget_scope_sha256,
            "call_id_sha256": call_id_sha256,
            "limits": normalized_limits,
        })
    canonical = _canonical_json_bytes(unsigned)
    payload = {
        **unsigned,
        "auth_hmac": hmac.new(
            auth_key,
            canonical,
            hashlib.sha256,
        ).hexdigest(),
    }
    rendered = POLICY_PREFACE_PREFIX + json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(rendered) > MAX_POLICY_PREFACE_BYTES:
        raise BridgeConfigurationError(
            "exact egress policy preface is too large"
        )
    return rendered


class ProxySocketAuthority:
    """Validate and connect to one deployment-owned Unix-domain socket."""

    def __init__(
        self,
        path: Path,
        *,
        expected_uid: int,
        expected_gid: int,
    ):
        self.path = path
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid

    def validate(self) -> None:
        parent = self.path.parent
        try:
            parent_info = parent.lstat()
            socket_info = self.path.lstat()
        except OSError as exc:
            raise BridgeConfigurationError(
                "fixed policy-proxy socket is unavailable"
            ) from exc
        if not stat.S_ISDIR(parent_info.st_mode) or parent.is_symlink():
            raise BridgeConfigurationError(
                "policy-proxy socket parent is not a real directory"
            )
        if parent_info.st_uid != self.expected_uid:
            raise BridgeConfigurationError(
                "policy-proxy socket parent has an unexpected owner"
            )
        if parent_info.st_gid != self.expected_gid:
            raise BridgeConfigurationError(
                "policy-proxy socket parent has an unexpected control group"
            )
        parent_mode = stat.S_IMODE(parent_info.st_mode)
        if not parent_mode & stat.S_ISGID:
            raise BridgeConfigurationError(
                "policy-proxy socket parent does not enforce group inheritance"
            )
        if (
            not parent_mode & stat.S_IXGRP
            or parent_mode & stat.S_IWGRP
            or parent_mode & 0o007
        ):
            raise BridgeConfigurationError(
                "policy-proxy socket parent has unsafe group/world permissions"
            )
        if not stat.S_ISSOCK(socket_info.st_mode) or self.path.is_symlink():
            raise BridgeConfigurationError(
                "fixed policy-proxy endpoint is not a Unix socket"
            )
        if socket_info.st_uid != self.expected_uid:
            raise BridgeConfigurationError(
                "policy-proxy socket has an unexpected owner"
            )
        if socket_info.st_gid != self.expected_gid:
            raise BridgeConfigurationError(
                "policy-proxy socket has an unexpected control group"
            )
        socket_mode = stat.S_IMODE(socket_info.st_mode)
        if socket_mode & 0o007 or socket_mode & 0o060 != 0o060:
            raise BridgeConfigurationError(
                "policy-proxy socket has unsafe group/world permissions"
            )

    def connect(self) -> socket.socket:
        self.validate()
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            upstream.connect(str(self.path))
            credentials = upstream.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                _SO_PEERCRED_SIZE,
            )
            _, peer_uid, _ = struct.unpack("3i", credentials)
            if peer_uid != self.expected_uid:
                raise BridgeConfigurationError(
                    "policy-proxy peer identity changed during connection"
                )
            upstream.settimeout(None)
            return upstream
        except BaseException:
            upstream.close()
            raise


@dataclass(frozen=True, slots=True)
class _TrustSnapshot:
    ca_content: bytes
    spki_content: bytes
    spki: str
    manifest_content: bytes
    generation_id: str


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


class ProxyTrustAuthority:
    """Validate proxy-owned public trust files and copy them per execution."""

    def __init__(
        self,
        ca_path: Path = PROXY_CA_CERTIFICATE_PATH,
        spki_path: Path = PROXY_LEAF_SPKI_PATH,
        manifest_path: Path = PROXY_TRUST_GENERATION_PATH,
        *,
        expected_uid: int,
        expected_gid: int,
    ) -> None:
        self.ca_path = ca_path
        self.spki_path = spki_path
        self.manifest_path = manifest_path
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid

    def _read_file(
        self,
        path: Path,
        *,
        maximum_bytes: int,
    ) -> bytes:
        if (
            not path.is_absolute()
            or path.parent != self.ca_path.parent
            or path.name
            not in {"ca.pem", "leaf.spki", "generation.json"}
        ):
            raise BridgeConfigurationError(
                "proxy trust path is not fixed"
            )
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise BridgeConfigurationError(
                "proxy trust material is unavailable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != self.expected_uid
                or before.st_gid != self.expected_gid
                or stat.S_IMODE(before.st_mode) != 0o440
                or not 1 <= before.st_size <= maximum_bytes
            ):
                raise BridgeConfigurationError(
                    "proxy trust material has unsafe metadata"
                )
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
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
                raise BridgeConfigurationError(
                    "proxy trust material changed during validation"
                )
            return content
        finally:
            os.close(descriptor)

    def _socket_authority(self) -> ProxySocketAuthority:
        return ProxySocketAuthority(
            self.ca_path.parent / "proxy.sock",
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )

    def _verify_socket_peer(self) -> None:
        connection = self._socket_authority().connect()
        connection.close()

    def _parse_manifest(self, content: bytes) -> dict[str, object]:
        try:
            payload = json.loads(content.decode("ascii", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BridgeConfigurationError(
                "proxy trust generation manifest is invalid"
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
            or payload.get("version")
            != TRUST_GENERATION_MANIFEST_VERSION
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
            raise BridgeConfigurationError(
                "proxy trust generation manifest is invalid"
            )
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
            raise BridgeConfigurationError(
                "proxy trust generation manifest is invalid"
            )
        return payload

    def _snapshot(self) -> _TrustSnapshot:
        self._verify_socket_peer()
        manifest_before = self._read_file(
            self.manifest_path,
            maximum_bytes=MAX_TRUST_GENERATION_MANIFEST_BYTES,
        )
        parsed = self._parse_manifest(manifest_before)
        socket_authority = ProxySocketAuthority(
            self.ca_path.parent / "proxy.sock",
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )
        socket_authority.validate()
        ca_content = self._read_file(
            self.ca_path,
            maximum_bytes=MAX_CA_CERTIFICATE_BYTES,
        )
        try:
            decoded_ca = ca_content.decode("ascii", errors="strict")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=decoded_ca)
        except (UnicodeError, ssl.SSLError) as exc:
            raise BridgeConfigurationError(
                "proxy CA certificate is invalid"
            ) from exc
        spki_content = self._read_file(
            self.spki_path,
            maximum_bytes=MAX_SPKI_FILE_BYTES,
        )
        try:
            spki = spki_content.decode("ascii", errors="strict").strip()
        except UnicodeError as exc:
            raise BridgeConfigurationError(
                "proxy leaf SPKI is invalid"
            ) from exc
        if (
            _SPKI_SHA256_RE.fullmatch(spki) is None
            or spki_content != (spki + "\n").encode("ascii")
        ):
            raise BridgeConfigurationError(
                "proxy leaf SPKI is invalid"
            )
        manifest_after = self._read_file(
            self.manifest_path,
            maximum_bytes=MAX_TRUST_GENERATION_MANIFEST_BYTES,
        )
        self._verify_socket_peer()
        if manifest_before != manifest_after:
            raise BridgeConfigurationError(
                "proxy trust generation changed during validation"
            )
        generation_id = _trust_generation_id(
            ca_content,
            spki_content,
        )
        if (
            parsed["generation_id"] != generation_id
            or parsed["ca_file_sha256"]
            != hashlib.sha256(ca_content).hexdigest()
            or parsed["leaf_spki_file_sha256"]
            != hashlib.sha256(spki_content).hexdigest()
        ):
            raise BridgeConfigurationError(
                "proxy trust generation contains mixed material"
            )
        return _TrustSnapshot(
            ca_content=ca_content,
            spki_content=spki_content,
            spki=spki,
            manifest_content=manifest_before,
            generation_id=generation_id,
        )

    def validate(self) -> tuple[bytes, str, str]:
        snapshot = self._snapshot()
        return (
            snapshot.ca_content,
            snapshot.spki,
            snapshot.generation_id,
        )

    def materialize(
        self,
        runtime_root: Path,
        *,
        worker_uid: int,
        worker_gid: int,
    ) -> dict[str, str]:
        snapshot_before = self._snapshot()
        try:
            root_info = runtime_root.lstat()
            resolved_root = runtime_root.resolve(strict=True)
        except OSError as exc:
            raise BridgeConfigurationError(
                "execution trust root is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(root_info.st_mode)
            or not stat.S_ISDIR(root_info.st_mode)
            or resolved_root != runtime_root
            or root_info.st_uid != worker_uid
            or root_info.st_gid != worker_gid
            or root_info.st_mode & 0o077
        ):
            raise BridgeConfigurationError(
                "execution trust root is unsafe"
            )
        trust_root = runtime_root / ".chatds-egress-trust"
        try:
            # The controller owns this directory and its files.  The worker
            # receives only group read/traverse access, so it cannot replace
            # the CA or SPKI with caller-controlled trust material.
            trust_root.mkdir(mode=0o700)
            os.chown(
                trust_root,
                os.geteuid(),
                worker_gid,
                follow_symlinks=False,
            )
            for name, content in (
                ("ca.pem", snapshot_before.ca_content),
                ("leaf.spki", snapshot_before.spki_content),
                ("generation.json", snapshot_before.manifest_content),
            ):
                target = trust_root / name
                descriptor = os.open(
                    target,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    view = memoryview(content)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    os.fsync(descriptor)
                    os.fchown(descriptor, os.geteuid(), worker_gid)
                    os.fchmod(descriptor, 0o440)
                finally:
                    os.close(descriptor)
            trust_root.chmod(0o550)
        except OSError as exc:
            raise BridgeConfigurationError(
                "execution trust material could not be copied"
            ) from exc
        snapshot_after = self._snapshot()
        if snapshot_before != snapshot_after:
            raise BridgeConfigurationError(
                "proxy trust generation changed while copying"
            )
        ca_copy = str(trust_root / "ca.pem")
        spki_copy = str(trust_root / "leaf.spki")
        manifest_copy = str(trust_root / "generation.json")
        return {
            "SSL_CERT_FILE": ca_copy,
            "REQUESTS_CA_BUNDLE": ca_copy,
            "CURL_CA_BUNDLE": ca_copy,
            "NODE_EXTRA_CA_CERTS": ca_copy,
            "GIT_SSL_CAINFO": ca_copy,
            "AWS_CA_BUNDLE": ca_copy,
            "SKILL_EGRESS_CA_CERT_PATH": ca_copy,
            "SKILL_EGRESS_LEAF_SPKI_PATH": spki_copy,
            "SKILL_EGRESS_TRUST_GENERATION_MANIFEST_PATH": (
                manifest_copy
            ),
            "SKILL_EGRESS_TRUST_GENERATION": (
                snapshot_before.generation_id
            ),
        }


@dataclass(frozen=True, slots=True)
class _RelayOutcome:
    clean_close: bool
    budget_rejected: bool


class _InvocationAudit:
    """Thread-safe local budget and disclosure-free audit accumulator."""

    def __init__(
        self,
        *,
        budget_scope_sha256: str,
        call_id_sha256: str,
        rules_sha256: str,
        limits: dict[str, int],
    ) -> None:
        self.budget_scope_sha256 = budget_scope_sha256
        self.call_id_sha256 = call_id_sha256
        self.rules_sha256 = rules_sha256
        self.limits = dict(limits)
        self._lock = threading.Lock()
        self._accepted_connections = 0
        self._client_to_proxy_wire_bytes = 0
        self._proxy_to_client_wire_bytes = 0
        self._reserved_outbound_bytes = 0
        self._reserved_response_bytes = 0
        self._budget_rejections = 0
        self._clean_closes = 0
        self._exhausted = False
        self._sealed_receipt: dict[str, object] | None = None

    def _require_mutable(self) -> None:
        if self._sealed_receipt is not None:
            raise BridgeConfigurationError(
                "bounded exchange audit is already sealed"
            )

    def try_accept_connection(self) -> bool:
        with self._lock:
            self._require_mutable()
            if (
                self._accepted_connections
                >= self.limits["max_requests"]
            ):
                self._budget_rejections += 1
                self._exhausted = True
                return False
            self._accepted_connections += 1
            if (
                self._accepted_connections
                >= self.limits["max_requests"]
            ):
                self._exhausted = True
            return True

    def reserve_wire_bytes(
        self,
        direction: str,
        amount: int,
    ) -> bool:
        if amount < 0:
            raise BridgeConfigurationError(
                "invalid bounded exchange byte reservation"
            )
        if amount == 0:
            return True
        with self._lock:
            self._require_mutable()
            if direction == "client_to_proxy":
                current = (
                    self._client_to_proxy_wire_bytes
                    + self._reserved_outbound_bytes
                )
                limit = self.limits["max_outbound_bytes"]
                if amount > limit - current:
                    self._budget_rejections += 1
                    self._exhausted = True
                    return False
                self._reserved_outbound_bytes += amount
                return True
            if direction == "proxy_to_client":
                current = (
                    self._proxy_to_client_wire_bytes
                    + self._reserved_response_bytes
                )
                limit = self.limits[
                    "max_response_wire_bytes"
                ]
                if amount > limit - current:
                    self._budget_rejections += 1
                    self._exhausted = True
                    return False
                self._reserved_response_bytes += amount
                return True
            raise BridgeConfigurationError(
                "invalid bounded exchange byte direction"
            )

    def commit_wire_bytes(
        self,
        direction: str,
        amount: int,
    ) -> None:
        if amount <= 0:
            return
        with self._lock:
            self._require_mutable()
            if direction == "client_to_proxy":
                if amount > self._reserved_outbound_bytes:
                    raise BridgeConfigurationError(
                        "bounded exchange outbound accounting drift"
                    )
                self._reserved_outbound_bytes -= amount
                self._client_to_proxy_wire_bytes += amount
                return
            if direction == "proxy_to_client":
                if amount > self._reserved_response_bytes:
                    raise BridgeConfigurationError(
                        "bounded exchange response accounting drift"
                    )
                self._reserved_response_bytes -= amount
                self._proxy_to_client_wire_bytes += amount
                return
            raise BridgeConfigurationError(
                "invalid bounded exchange byte direction"
            )

    def release_wire_bytes(
        self,
        direction: str,
        amount: int,
    ) -> None:
        if amount <= 0:
            return
        with self._lock:
            self._require_mutable()
            if direction == "client_to_proxy":
                if amount > self._reserved_outbound_bytes:
                    raise BridgeConfigurationError(
                        "bounded exchange outbound accounting drift"
                    )
                self._reserved_outbound_bytes -= amount
                return
            if direction == "proxy_to_client":
                if amount > self._reserved_response_bytes:
                    raise BridgeConfigurationError(
                        "bounded exchange response accounting drift"
                    )
                self._reserved_response_bytes -= amount
                return
            raise BridgeConfigurationError(
                "invalid bounded exchange byte direction"
            )

    def record_clean_close(self) -> None:
        with self._lock:
            self._require_mutable()
            self._clean_closes += 1

    @staticmethod
    def _copy_receipt(
        receipt: dict[str, object],
    ) -> dict[str, object]:
        return json.loads(
            _canonical_json_bytes(receipt).decode("utf-8")
        )

    def seal(self) -> dict[str, object]:
        with self._lock:
            if self._sealed_receipt is not None:
                return self._copy_receipt(self._sealed_receipt)
            if (
                self._reserved_outbound_bytes
                or self._reserved_response_bytes
            ):
                raise BridgeConfigurationError(
                    "bounded exchange audit has unsettled byte reservations"
                )
            counts: dict[str, object] = {
                "accepted_connections": (
                    self._accepted_connections
                ),
                "client_to_proxy_wire_bytes": (
                    self._client_to_proxy_wire_bytes
                ),
                "proxy_to_client_wire_bytes": (
                    self._proxy_to_client_wire_bytes
                ),
                "budget_rejections": self._budget_rejections,
                "clean_closes": self._clean_closes,
            }
            limits = dict(self.limits)
            exhausted = bool(
                self._exhausted
                or (
                    self._accepted_connections
                    >= limits["max_requests"]
                )
                or (
                    self._client_to_proxy_wire_bytes
                    + self._reserved_outbound_bytes
                    >= limits["max_outbound_bytes"]
                )
                or (
                    self._proxy_to_client_wire_bytes
                    + self._reserved_response_bytes
                    >= limits["max_response_wire_bytes"]
                )
            )
            receipt: dict[str, object] = {
                "profile": BOUNDED_EXCHANGE_PROFILE,
                "version": AUDIT_RECEIPT_VERSION,
                "budget_scope_sha256": self.budget_scope_sha256,
                "call_id_sha256": self.call_id_sha256,
                "rules_sha256": self.rules_sha256,
                "counts": counts,
                "limits": limits,
                "exhausted": exhausted,
            }
            receipt["receipt_sha256"] = _canonical_json_sha256(
                receipt
            )
            self._sealed_receipt = receipt
            return self._copy_receipt(receipt)

    def receipt(self) -> dict[str, object]:
        with self._lock:
            if self._sealed_receipt is None:
                raise BridgeConfigurationError(
                    "bounded exchange audit is not sealed"
                )
            return self._copy_receipt(self._sealed_receipt)


def _relay(
    client: socket.socket,
    upstream: socket.socket,
    *,
    audit: _InvocationAudit | None = None,
    stop_event: threading.Event | None = None,
) -> _RelayOutcome:
    """Relay bounded byte streams in both directions without parsing targets."""

    peers = {client: upstream, upstream: client}
    read_open = {client: True, upstream: True}
    write_shutdown = {client: False, upstream: False}
    pending = {client: bytearray(), upstream: bytearray()}
    selector = selectors.DefaultSelector()
    for endpoint in peers:
        endpoint.setblocking(False)
        selector.register(endpoint, selectors.EVENT_READ)
    last_activity = time.monotonic()

    def refresh(endpoint: socket.socket) -> None:
        events = selectors.EVENT_READ if read_open[endpoint] else 0
        if pending[endpoint]:
            events |= selectors.EVENT_WRITE
        try:
            if events:
                selector.modify(endpoint, events)
            else:
                selector.unregister(endpoint)
        except KeyError:
            if events:
                selector.register(endpoint, events)

    def finish_read(endpoint: socket.socket) -> None:
        """Close one read direction after EOF/reset and drain its reverse."""

        if not read_open[endpoint]:
            return
        read_open[endpoint] = False
        refresh(endpoint)
        peer = peers[endpoint]
        if not pending[peer] and not write_shutdown[peer]:
            try:
                peer.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            write_shutdown[peer] = True

    def direction_for_destination(endpoint: socket.socket) -> str:
        return (
            "client_to_proxy"
            if endpoint is upstream
            else "proxy_to_client"
        )

    def release_pending(endpoint: socket.socket) -> None:
        amount = len(pending[endpoint])
        if amount and audit is not None:
            audit.release_wire_bytes(
                direction_for_destination(endpoint),
                amount,
            )
        pending[endpoint].clear()

    def finish_write(endpoint: socket.socket) -> None:
        """Abandon one failed write without discarding reverse traffic."""

        release_pending(endpoint)
        write_shutdown[endpoint] = True
        source = peers[endpoint]
        finish_read(source)
        refresh(endpoint)

    transport_error = False
    try:
        while selector.get_map():
            if stop_event is not None and stop_event.is_set():
                return _RelayOutcome(False, False)
            remaining = IDLE_TIMEOUT_SECONDS - (time.monotonic() - last_activity)
            if remaining <= 0:
                return _RelayOutcome(False, False)
            events = selector.select(timeout=min(
                0.1 if stop_event is not None else 1.0,
                remaining,
            ))
            if not events:
                continue
            for key, mask in events:
                endpoint = key.fileobj
                if not isinstance(endpoint, socket.socket):
                    return _RelayOutcome(False, False)
                peer = peers[endpoint]
                if mask & selectors.EVENT_READ:
                    try:
                        chunk = endpoint.recv(64 * 1024)
                    except BlockingIOError:
                        chunk = None
                    except OSError:
                        # A reset closes only this read direction. In
                        # particular, an authenticated-policy rejection may
                        # race a browser request already queued in the other
                        # direction; preserve and flush any typed response
                        # already received from the proxy.
                        transport_error = True
                        finish_read(endpoint)
                        chunk = None
                    if chunk:
                        direction = direction_for_destination(peer)
                        if (
                            audit is not None
                            and not audit.reserve_wire_bytes(
                                direction,
                                len(chunk),
                            )
                        ):
                            return _RelayOutcome(False, True)
                        if (
                            len(pending[peer]) + len(chunk)
                            > MAX_DIRECTION_BUFFER_BYTES
                        ):
                            if audit is not None:
                                audit.release_wire_bytes(
                                    direction,
                                    len(chunk),
                                )
                            return _RelayOutcome(False, False)
                        pending[peer].extend(chunk)
                        last_activity = time.monotonic()
                        refresh(peer)
                    elif chunk == b"":
                        finish_read(endpoint)
                if mask & selectors.EVENT_WRITE and pending[endpoint]:
                    try:
                        sent = endpoint.send(pending[endpoint])
                    except BlockingIOError:
                        sent = 0
                    except OSError:
                        # Stop only this write direction. The policy proxy can
                        # reject the authenticated preface before consuming
                        # the browser's queued request, in which case its
                        # typed HTTP error may already be pending in the
                        # opposite direction. Preserve and flush that error
                        # instead of returning on the expected broken pipe.
                        transport_error = True
                        finish_write(endpoint)
                        continue
                    if sent:
                        if audit is not None:
                            audit.commit_wire_bytes(
                                direction_for_destination(endpoint),
                                sent,
                            )
                        del pending[endpoint][:sent]
                        last_activity = time.monotonic()
                        refresh(endpoint)
                        source = peers[endpoint]
                        if (
                            not pending[endpoint]
                            and not read_open[source]
                            and not write_shutdown[endpoint]
                        ):
                            try:
                                endpoint.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass
                            write_shutdown[endpoint] = True
                    elif sent == 0:
                        # A non-empty nonblocking socket write is not expected
                        # to return zero. Treat it as a terminal condition for
                        # this direction so a permanently writable descriptor
                        # cannot spin while reverse traffic remains pending.
                        transport_error = True
                        finish_write(endpoint)
            if (
                not any(read_open.values())
                and not any(pending.values())
            ):
                return _RelayOutcome(
                    not transport_error,
                    False,
                )
    finally:
        for endpoint in tuple(pending):
            release_pending(endpoint)
        selector.close()


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, LoopbackProxyBridge):
            return
        try:
            upstream = server.proxy_authority.connect()
        except (BridgeConfigurationError, OSError):
            return
        try:
            server._register_upstream(self.request, upstream)
            server._require_stable_authority_projection()
            preface = _policy_preface(
                server.origin_allowlist,
                egress_rules=server.egress_rules,
                private_origins=server.private_origins,
                public_read=server.public_read,
                auth_key=server.policy_auth_key,
                trust_generation=server.trust_generation,
                budget_scope_sha256=(
                    server.budget_scope_sha256
                ),
                call_id_sha256=server.call_id_sha256,
                limits=server._policy_limits_payload(),
            )
            upstream.sendall(preface)
            outcome = _relay(
                self.request,
                upstream,
                audit=server._invocation_audit,
                stop_event=server._handler_stop_event,
            )
            if outcome.clean_close:
                server._record_clean_close()
        except (BridgeConfigurationError, OSError):
            return
        finally:
            upstream.close()


class LoopbackProxyBridge(
    socketserver.ThreadingMixIn,
    socketserver.TCPServer,
):
    """Bounded controller service with a fixed loopback listener."""

    address_family = socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(
        self,
        proxy_authority: ProxySocketAuthority,
        server_address: tuple[str, int] = (LISTEN_HOST, LISTEN_PORT),
        *,
        origin_allowlist: tuple[str, ...] = (),
        egress_rules: tuple[dict[str, object], ...] = (),
        private_origins: tuple[str, ...] = (),
        public_read: dict[str, object] | None = None,
        policy_token: str | None = None,
        trust_generation: str,
        budget_scope_sha256: str | None = None,
        call_id_sha256: str | None = None,
        limits: dict[str, object] | None = None,
    ):
        if server_address[0] != LISTEN_HOST:
            raise BridgeConfigurationError(
                "policy bridge may only bind the IPv4 loopback address"
            )
        self.proxy_authority = proxy_authority
        (
            normalized,
            normalized_rules,
            normalized_private_origins,
        ) = _validated_exact_policy(
            origin_allowlist,
            egress_rules,
            private_origins,
        )
        self.origin_allowlist = normalized
        self.egress_rules = normalized_rules
        self.private_origins = normalized_private_origins
        self.public_read = _validated_public_read_profile(public_read)
        self._rules_sha256 = _authority_projection_sha256(
            self.origin_allowlist,
            self.egress_rules,
            self.private_origins,
            self.public_read,
        )
        self.policy_auth_key = _policy_auth_key(policy_token)
        v3_requested = any(
            value is not None
            for value in (
                budget_scope_sha256,
                call_id_sha256,
                limits,
            )
        )
        if v3_requested:
            if (
                not isinstance(budget_scope_sha256, str)
                or _SHA256_HEX_RE.fullmatch(
                    budget_scope_sha256
                ) is None
                or not isinstance(call_id_sha256, str)
                or _SHA256_HEX_RE.fullmatch(
                    call_id_sha256
                ) is None
            ):
                raise BridgeConfigurationError(
                    "invalid bounded exchange identity"
                )
            normalized_limits = _validated_v3_limits(limits)
            self.budget_scope_sha256 = budget_scope_sha256
            self.call_id_sha256 = call_id_sha256
            self._limits: dict[str, int] | None = dict(
                normalized_limits
            )
            self.policy_version = 3
            self._invocation_audit: _InvocationAudit | None = (
                _InvocationAudit(
                    budget_scope_sha256=budget_scope_sha256,
                    call_id_sha256=call_id_sha256,
                    rules_sha256=self._rules_sha256,
                    limits=normalized_limits,
                )
            )
        else:
            self.budget_scope_sha256 = None
            self.call_id_sha256 = None
            self._limits = None
            self.policy_version = 2
            self._invocation_audit = None
        if _SHA256_HEX_RE.fullmatch(trust_generation) is None:
            raise BridgeConfigurationError(
                "proxy trust generation is unavailable"
            )
        self.trust_generation = trust_generation
        # Detect a policy which cannot fit the proxy's bounded preface before
        # accepting any browser connection.
        _policy_preface(
            self.origin_allowlist,
            egress_rules=self.egress_rules,
            private_origins=self.private_origins,
            public_read=self.public_read,
            auth_key=self.policy_auth_key,
            trust_generation=self.trust_generation,
            budget_scope_sha256=self.budget_scope_sha256,
            call_id_sha256=self.call_id_sha256,
            limits=self._policy_limits_payload(),
        )
        self._admission = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._lifecycle_condition = threading.Condition(
            threading.RLock()
        )
        self._active_connections: dict[
            socket.socket, set[socket.socket]
        ] = {}
        self._handler_stop_event = threading.Event()
        self._closing = False
        self._sealed = False
        super().__init__(server_address, _BridgeHandler)

    def verify_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> bool:
        if client_address[0] != LISTEN_HOST:
            return False
        with self._lifecycle_condition:
            if self._closing:
                return False
            if not self._admission.acquire(blocking=False):
                return False
            audit = self._invocation_audit
            try:
                if (
                    audit is not None
                    and not audit.try_accept_connection()
                ):
                    self._admission.release()
                    return False
                self._active_connections[request] = {request}
                return True
            except BaseException:
                self._admission.release()
                raise

    def _register_upstream(
        self,
        request: socket.socket,
        upstream: socket.socket,
    ) -> None:
        with self._lifecycle_condition:
            sockets = self._active_connections.get(request)
            if self._closing or sockets is None:
                try:
                    upstream.close()
                finally:
                    raise BridgeConfigurationError(
                        "bounded exchange is closing"
                    )
            sockets.add(upstream)

    def _unregister_connection(
        self,
        request: socket.socket,
    ) -> None:
        with self._lifecycle_condition:
            self._active_connections.pop(request, None)
            self._lifecycle_condition.notify_all()

    @staticmethod
    def _close_connection_socket(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def shutdown_and_seal(
        self,
        *,
        timeout_seconds: float = HANDLER_DRAIN_TIMEOUT_SECONDS,
    ) -> dict[str, object] | None:
        """Stop admission, drain every handler, then freeze the v3 receipt."""

        if timeout_seconds <= 0:
            raise BridgeConfigurationError(
                "invalid bridge handler drain timeout"
            )
        with self._lifecycle_condition:
            if self._sealed:
                return self.audit_receipt()
            self._closing = True
            self._handler_stop_event.set()

        # Wake and stop the accept loop before taking the final active-socket
        # snapshot. BaseServer.shutdown waits until serve_forever has exited,
        # so no verified request can be handed to process_request afterward.
        self.shutdown()
        self.server_close()
        with self._lifecycle_condition:
            active = {
                connection
                for sockets in self._active_connections.values()
                for connection in sockets
            }
        for connection in active:
            self._close_connection_socket(connection)

        deadline = time.monotonic() + timeout_seconds
        with self._lifecycle_condition:
            while self._active_connections:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeConfigurationError(
                        "bounded exchange handlers did not drain"
                    )
                self._lifecycle_condition.wait(remaining)

        audit = self._invocation_audit
        receipt = None if audit is None else audit.seal()
        with self._lifecycle_condition:
            self._sealed = True
        return receipt

    def _record_clean_close(self) -> None:
        audit = self._invocation_audit
        if audit is not None:
            audit.record_clean_close()

    def _policy_limits_payload(self) -> dict[str, object] | None:
        limits = self._limits
        return None if limits is None else dict(limits)

    def _require_stable_authority_projection(self) -> None:
        if not hmac.compare_digest(
            self._rules_sha256,
            _authority_projection_sha256(
                self.origin_allowlist,
                self.egress_rules,
                self.private_origins,
                self.public_read,
            ),
        ):
            raise BridgeConfigurationError(
                "bounded exchange authority changed after admission"
            )

    def audit_receipt(self) -> dict[str, object] | None:
        """Return the immutable content-only v3 terminal receipt.

        Legacy version-2 bridges intentionally return ``None``: they have no
        stable call identity or per-call budget to bind an audit receipt.
        """

        audit = self._invocation_audit
        return None if audit is None else audit.receipt()

    def process_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._admission.release()
            self._unregister_connection(request)
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._unregister_connection(request)
            self._admission.release()


def main(arguments: list[str] | None = None) -> int:
    received = list(sys.argv[1:] if arguments is None else arguments)
    if received:
        print("proxy bridge does not accept runtime arguments", file=sys.stderr)
        return 64
    if os.geteuid() != 0:
        print("proxy bridge must run as the controller identity", file=sys.stderr)
        return 78
    authority = ProxySocketAuthority(
        PROXY_SOCKET_PATH,
        expected_uid=EXPECTED_PROXY_UID,
        expected_gid=EXPECTED_BRIDGE_GID,
    )
    try:
        authority.validate()
        _ca_content, _spki, trust_generation = (
            ProxyTrustAuthority(
                PROXY_CA_CERTIFICATE_PATH,
                PROXY_LEAF_SPKI_PATH,
                PROXY_TRUST_GENERATION_PATH,
                expected_uid=EXPECTED_PROXY_UID,
                expected_gid=EXPECTED_BRIDGE_GID,
            ).validate()
        )
        with LoopbackProxyBridge(
            authority,
            origin_allowlist=(),
            trust_generation=trust_generation,
        ) as server:
            server.serve_forever(poll_interval=0.25)
    except (BridgeConfigurationError, OSError) as exc:
        print(f"proxy bridge startup failed: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
