"""Small fail-closed HTTP CONNECT proxy for isolated Skill browser workers.

The browser worker is attached only to an internal Docker network.  This
service is the sole member of that network with external egress, so changing
browser flags or ignoring proxy environment variables cannot create a direct
network path.  Every destination is resolved and classified here before a
connection is made to one pinned address.

This is deliberately not a general forward proxy.  It supports the two forms
used by browsers (CONNECT for HTTPS and absolute-form HTTP), limits ports,
blocks non-public addresses by default, and never logs URL paths.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import selectors
import socket
import socketserver
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit


LISTEN_HOST: Final[str] = os.environ.get("SKILL_EGRESS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT: Final[int] = int(os.environ.get("SKILL_EGRESS_LISTEN_PORT", "8080"))
LISTEN_SOCKET: Final[str] = os.environ.get("SKILL_EGRESS_SOCKET_PATH", "").strip()
CONNECT_TIMEOUT_SECONDS: Final[float] = float(
    os.environ.get("SKILL_EGRESS_CONNECT_TIMEOUT_SECONDS", "10")
)
IDLE_TIMEOUT_SECONDS: Final[float] = float(
    os.environ.get("SKILL_EGRESS_IDLE_TIMEOUT_SECONDS", "30")
)
MAX_TUNNEL_SECONDS: Final[float] = float(
    os.environ.get("SKILL_EGRESS_MAX_TUNNEL_SECONDS", "600")
)
MAX_HEADER_BYTES: Final[int] = 64 * 1024
MAX_BUFFER_BYTES: Final[int] = 256 * 1024
COPY_CHUNK_BYTES: Final[int] = 64 * 1024
MAX_CONCURRENT_CONNECTIONS: Final[int] = int(
    os.environ.get("SKILL_EGRESS_MAX_CONCURRENT_CONNECTIONS", "64")
)
_NAT64_TRANSITION_NETWORKS: Final[tuple[ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


class ProxyPolicyError(ValueError):
    """A stable destination or request-policy rejection."""


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
    host = value.rstrip(".").casefold()
    if not host or "\x00" in host or any(ord(char) < 0x20 for char in host):
        raise ProxyPolicyError("invalid_destination_host")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ProxyPolicyError("invalid_destination_host") from exc


def _origin_tuple(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProxyPolicyError("invalid_private_origin_allowlist")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ProxyPolicyError("invalid_private_origin_allowlist") from exc
    return parsed.scheme, _normalized_host(parsed.hostname), port


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

    def resolve(self, scheme: str, host: str, port: int) -> Destination:
        if scheme not in {"http", "https"}:
            raise ProxyPolicyError("unsupported_destination_scheme")
        normalized_host = _normalized_host(host)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ProxyPolicyError("invalid_destination_port")
        private_grant = (
            scheme,
            normalized_host,
            port,
        ) in self.private_origins
        if not private_grant and port not in self.public_ports:
            raise ProxyPolicyError("destination_port_not_allowed")

        try:
            records = socket.getaddrinfo(
                normalized_host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise ProxyPolicyError("destination_dns_failed") from exc
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
            raise ProxyPolicyError("destination_dns_empty")

        classifications = [ipaddress.ip_address(address) for _, address in candidates]
        if not private_grant and any(
            not _is_public_unicast(address) for address in classifications
        ):
            # Reject a mixed public/private answer rather than selecting only
            # the public member.  This closes DNS-rebinding and split-horizon
            # ambiguity at the resolution boundary.
            raise ProxyPolicyError("destination_address_not_public")
        if private_grant:
            # An origin grant alone is not sufficient: every DNS answer must
            # also remain in an explicitly configured address range. This
            # keeps an allowed hostname from rebinding to metadata or another
            # internal segment.
            if (
                not self.private_cidrs
                or any(
                    not any(address in network for network in self.private_cidrs)
                    for address in classifications
                )
            ):
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
            scheme=scheme,
            host=normalized_host,
            port=port,
            address=selected_address,
            family=selected_family,
            private_grant=private_grant,
        )


def _read_headers(connection: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(min(COPY_CHUNK_BYTES, MAX_HEADER_BYTES - len(data)))
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
    return host, port


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
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise ProxyPolicyError("unsupported_http_version")
    if method == "CONNECT":
        host, port = _parse_connect_target(target)
        return "https", host, port, b""

    parsed = urlsplit(target)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
    ):
        raise ProxyPolicyError("absolute_http_url_required")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise ProxyPolicyError("invalid_destination_port") from exc
    origin_target = parsed.path or "/"
    if parsed.query:
        origin_target += "?" + parsed.query
    first_line = b" ".join(
        (
            method_raw,
            origin_target.encode("ascii"),
            version_raw,
        )
    )
    filtered: list[bytes] = []
    for line in lines[1:]:
        name, separator, _value = line.partition(b":")
        if not separator:
            raise ProxyPolicyError("invalid_request_header")
        if name.strip().lower() in {
            b"proxy-authorization",
            b"proxy-connection",
        }:
            continue
        filtered.append(line)
    forwarded = b"\r\n".join([first_line, *filtered]) + b"\r\n\r\n" + body
    return "http", parsed.hostname, port, forwarded


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


class ProxyHandler(socketserver.BaseRequestHandler):
    policy = AddressPolicy()

    def handle(self) -> None:
        upstream: socket.socket | None = None
        try:
            request = _read_headers(self.request)
            scheme, host, port, forwarded = _request_destination(request)
            destination = self.policy.resolve(scheme, host, port)
            upstream = _connect_pinned(destination)
            if forwarded:
                upstream.sendall(forwarded)
            else:
                self.request.sendall(
                    b"HTTP/1.1 200 Connection Established\r\n"
                    b"Proxy-Agent: chatds-skill-egress\r\n\r\n"
                )
            upstream.setblocking(False)
            self.request.setblocking(False)
            _relay(self.request, upstream)
        except ProxyPolicyError as exc:
            _safe_error(self.request, 403, str(exc))
        except (ConnectionError, OSError, TimeoutError):
            _safe_error(self.request, 502, "destination_connection_failed")
        finally:
            if upstream is not None:
                upstream.close()


class _BoundedThreadingServer:
    """Shared bounded-admission behavior for TCP and Unix listeners."""

    def __init__(self, *args, **kwargs):
        if MAX_CONCURRENT_CONNECTIONS < 1:
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
    try:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
