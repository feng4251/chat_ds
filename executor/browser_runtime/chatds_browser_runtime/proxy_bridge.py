"""Controller-owned loopback bridge to the fixed Skill egress proxy socket.

The untrusted browser worker has no network interface beyond loopback.  It can
only reach this fixed TCP listener, which relays bytes to the policy proxy's
fixed Unix-domain socket.  The bridge deliberately has no URL, host, port, or
socket-path input controlled by the Skill process.
"""

from __future__ import annotations

import os
from pathlib import Path
import selectors
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
from typing import Final


LISTEN_HOST: Final[str] = "127.0.0.1"
LISTEN_PORT: Final[int] = 18080
PROXY_SOCKET_PATH: Final[Path] = Path(
    "/run/chatds-skill-egress/proxy.sock"
)
EXPECTED_PROXY_UID: Final[int] = 65531
EXPECTED_BRIDGE_GID: Final[int] = 65530
MAX_CONNECTIONS: Final[int] = 64
MAX_DIRECTION_BUFFER_BYTES: Final[int] = 1024 * 1024
IDLE_TIMEOUT_SECONDS: Final[float] = 660.0
CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0
_SO_PEERCRED_SIZE: Final[int] = struct.calcsize("3i")


class BridgeConfigurationError(RuntimeError):
    """The deployment-owned proxy socket boundary is not trustworthy."""


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


def _relay(client: socket.socket, upstream: socket.socket) -> None:
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

    try:
        while selector.get_map():
            remaining = IDLE_TIMEOUT_SECONDS - (time.monotonic() - last_activity)
            if remaining <= 0:
                return
            events = selector.select(timeout=min(1.0, remaining))
            if not events:
                continue
            for key, mask in events:
                endpoint = key.fileobj
                if not isinstance(endpoint, socket.socket):
                    return
                peer = peers[endpoint]
                if mask & selectors.EVENT_READ:
                    try:
                        chunk = endpoint.recv(64 * 1024)
                    except BlockingIOError:
                        chunk = None
                    except OSError:
                        # A browser may cancel an in-flight request by resetting
                        # either half of the relay.  This is local connection
                        # lifecycle, not a proxy-authority/configuration error.
                        return
                    if chunk:
                        if (
                            len(pending[peer]) + len(chunk)
                            > MAX_DIRECTION_BUFFER_BYTES
                        ):
                            return
                        pending[peer].extend(chunk)
                        last_activity = time.monotonic()
                        refresh(peer)
                    elif chunk == b"":
                        read_open[endpoint] = False
                        refresh(endpoint)
                        if not pending[peer] and not write_shutdown[peer]:
                            try:
                                peer.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass
                            write_shutdown[peer] = True
                if mask & selectors.EVENT_WRITE and pending[endpoint]:
                    try:
                        sent = endpoint.send(pending[endpoint])
                    except BlockingIOError:
                        sent = 0
                    except OSError:
                        # Treat a reset/broken pipe exactly like an ordinary
                        # peer close and end only this relay thread.
                        return
                    if sent:
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
            if (
                not any(read_open.values())
                and not any(pending.values())
            ):
                return
    finally:
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
            _relay(self.request, upstream)
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
    ):
        if server_address[0] != LISTEN_HOST:
            raise BridgeConfigurationError(
                "policy bridge may only bind the IPv4 loopback address"
            )
        self.proxy_authority = proxy_authority
        self._admission = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(server_address, _BridgeHandler)

    def verify_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> bool:
        del request
        if client_address[0] != LISTEN_HOST:
            return False
        return self._admission.acquire(blocking=False)

    def process_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._admission.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
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
        with LoopbackProxyBridge(authority) as server:
            server.serve_forever(poll_interval=0.25)
    except (BridgeConfigurationError, OSError) as exc:
        print(f"proxy bridge startup failed: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
