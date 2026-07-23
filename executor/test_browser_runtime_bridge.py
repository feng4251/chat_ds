from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "executor"))

from browser_runtime.chatds_browser_runtime.proxy_bridge import (
    BridgeConfigurationError,
    LoopbackProxyBridge,
    ProxySocketAuthority,
    _relay,
    main,
)
from skill_egress_proxy import server as proxy_server


class BrowserRuntimeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.parent.chmod(0o2710)
        self.socket_path = self.parent / "proxy.sock"
        self.servers = []
        self.threads = []

    def tearDown(self):
        for server in reversed(self.servers):
            server.shutdown()
            server.server_close()
        for thread in reversed(self.threads):
            thread.join(timeout=3)
        self.temporary.cleanup()

    def _start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        self.threads.append(thread)
        return server

    def _start_proxy_and_bridge(self, handler):
        proxy = self._start(
            proxy_server.ThreadingUnixProxyServer(
                str(self.socket_path),
                handler,
            )
        )
        self.socket_path.chmod(0o660)
        authority = ProxySocketAuthority(
            self.socket_path,
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )
        bridge = self._start(
            LoopbackProxyBridge(
                authority,
                ("127.0.0.1", 0),
            )
        )
        return int(bridge.server_address[1])

    @staticmethod
    def _request(port: int, request: bytes) -> bytes:
        response = bytearray()
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(request)
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        return bytes(response)

    def test_fixed_bridge_relays_policy_rejection(self):
        port = self._start_proxy_and_bridge(proxy_server.ProxyHandler)
        response = self._request(
            port,
            b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n",
        )
        self.assertTrue(response.startswith(b"HTTP/1.1 403 "))
        self.assertIn(b"destination_address_not_public", response)

    def test_fixed_bridge_relays_explicit_private_origin_grant(self):
        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"bridge-policy-proof"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        origin = self._start(
            ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
        )
        origin_port = int(origin.server_address[1])

        class GrantedProxyHandler(proxy_server.ProxyHandler):
            policy = proxy_server.AddressPolicy(
                public_ports=(80, 443),
                private_origins=(f"http://127.0.0.1:{origin_port}",),
                private_cidrs=("127.0.0.1/32",),
            )

        port = self._start_proxy_and_bridge(GrantedProxyHandler)
        response = self._request(
            port,
            (
                f"GET http://127.0.0.1:{origin_port}/proof HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{origin_port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"),
        )
        self.assertIn(b"HTTP/1.0 200 OK", response)
        self.assertTrue(response.endswith(b"bridge-policy-proof"))

    def test_relay_treats_peer_broken_pipe_as_connection_teardown(self):
        client, client_peer = socket.socketpair()
        upstream, upstream_peer = socket.socketpair()
        try:
            # Queue bytes for the upstream half, then make its next send hit a
            # broken pipe.  Normal browser cancellation must not escape the
            # per-connection relay and produce a socketserver traceback.
            client_peer.sendall(b"cancelled-request")
            upstream_peer.close()
            _relay(client, upstream)
        finally:
            client.close()
            client_peer.close()
            upstream.close()

    def test_authority_rejects_worker_accessible_parent_or_socket(self):
        proxy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            proxy.bind(str(self.socket_path))
            proxy.listen(1)
            self.socket_path.chmod(0o660)
            authority = ProxySocketAuthority(
                self.socket_path,
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
            )
            authority.validate()
            self.parent.chmod(0o2730)
            with self.assertRaises(BridgeConfigurationError):
                authority.validate()
            self.parent.chmod(0o2710)
            self.socket_path.chmod(0o666)
            with self.assertRaises(BridgeConfigurationError):
                authority.validate()
        finally:
            proxy.close()

    def test_bridge_rejects_non_loopback_bind_and_runtime_arguments(self):
        with self.assertRaises(BridgeConfigurationError):
            LoopbackProxyBridge(
                ProxySocketAuthority(
                    self.socket_path,
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                ),
                ("0.0.0.0", 0),
            )
        self.assertEqual(64, main(["--target", "example.com"]))


if __name__ == "__main__":
    unittest.main()
