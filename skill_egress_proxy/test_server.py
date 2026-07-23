from __future__ import annotations

import socket
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from skill_egress_proxy import server


class DestinationParsingTests(unittest.TestCase):
    def test_connect_and_http_absolute_forms(self):
        self.assertEqual(
            ("https", "example.com", 443, b""),
            server._request_destination(
                b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"
            ),
        )
        scheme, host, port, forwarded = server._request_destination(
            b"GET http://example.com/a?q=1 HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Proxy-Authorization: secret\r\n\r\n"
        )
        self.assertEqual(("http", "example.com", 80), (scheme, host, port))
        self.assertTrue(forwarded.startswith(b"GET /a?q=1 HTTP/1.1\r\n"))
        self.assertNotIn(b"Proxy-Authorization", forwarded)

    def test_credentials_and_non_absolute_http_are_rejected(self):
        with self.assertRaises(server.ProxyPolicyError):
            server._request_destination(
                b"GET /relative HTTP/1.1\r\nHost: example.com\r\n\r\n"
            )
        with self.assertRaises(server.ProxyPolicyError):
            server._request_destination(
                b"GET http://user:password@example.com/ HTTP/1.1\r\n\r\n"
            )


class AddressPolicyTests(unittest.TestCase):
    @staticmethod
    def _records(*addresses: str):
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (
                    (address, 443, 0, 0)
                    if ":" in address
                    else (address, 443)
                ),
            )
            for address in addresses
        ]

    def test_public_destination_is_pinned(self):
        policy = server.AddressPolicy(public_ports=(80, 443))
        with patch.object(
            server.socket,
            "getaddrinfo",
            return_value=self._records("93.184.216.34"),
        ):
            result = policy.resolve("https", "EXAMPLE.com.", 443)
        self.assertEqual("example.com", result.host)
        self.assertEqual("93.184.216.34", result.address)
        self.assertFalse(result.private_grant)

    def test_private_mixed_and_metadata_addresses_are_blocked(self):
        policy = server.AddressPolicy(public_ports=(443,))
        for addresses in (
            ("127.0.0.1",),
            ("169.254.169.254",),
            ("10.1.2.3",),
            ("0.0.0.0",),
            ("224.0.0.1",),
            ("239.255.255.250",),
            ("255.255.255.255",),
            ("::",),
            ("ff02::1",),
            ("::ffff:127.0.0.1",),
            ("2002:7f00:1::",),
            ("2001:0000:4136:e378:8000:63bf:3fff:fdd2",),
            ("64:ff9b::7f00:1",),
            ("64:ff9b:1::7f00:1",),
            ("93.184.216.34", "10.1.2.3"),
        ):
            with self.subTest(addresses=addresses), patch.object(
                server.socket,
                "getaddrinfo",
                return_value=self._records(*addresses),
            ):
                with self.assertRaisesRegex(
                    server.ProxyPolicyError,
                    "destination_address_not_public",
                ):
                    policy.resolve("https", "example.test", 443)

    def test_exact_private_origin_grant_does_not_widen_port_or_scheme(self):
        policy = server.AddressPolicy(
            public_ports=(80, 443),
            private_origins=("https://internal.example:8443",),
            private_cidrs=("10.0.0.0/8",),
        )
        with patch.object(
            server.socket,
            "getaddrinfo",
            return_value=self._records("10.1.2.3"),
        ):
            granted = policy.resolve(
                "https",
                "internal.example",
                8443,
            )
            self.assertTrue(granted.private_grant)
            with self.assertRaises(server.ProxyPolicyError):
                policy.resolve("http", "internal.example", 8443)

    def test_private_origin_requires_every_answer_in_explicit_cidr(self):
        policy = server.AddressPolicy(
            public_ports=(80, 443),
            private_origins=("https://internal.example:8443",),
            private_cidrs=("10.0.0.0/8",),
        )
        for addresses in (
            ("169.254.169.254",),
            ("10.1.2.3", "192.168.1.2"),
            ("10.1.2.3", "93.184.216.34"),
        ):
            with self.subTest(addresses=addresses), patch.object(
                server.socket,
                "getaddrinfo",
                return_value=self._records(*addresses),
            ):
                with self.assertRaisesRegex(
                    server.ProxyPolicyError,
                    "destination_address_outside_private_cidr_allowlist",
                ):
                    policy.resolve("https", "internal.example", 8443)

    def test_public_non_web_port_is_blocked_before_dns(self):
        policy = server.AddressPolicy(public_ports=(80, 443))
        with patch.object(server.socket, "getaddrinfo") as resolver:
            with self.assertRaisesRegex(
                server.ProxyPolicyError,
                "destination_port_not_allowed",
            ):
                policy.resolve("https", "example.com", 22)
        resolver.assert_not_called()


class ProxyRelayIntegrationTests(unittest.TestCase):
    def test_private_origin_grant_relays_one_absolute_http_request(self):
        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"deterministic-origin"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
        origin_port = int(origin.server_address[1])

        class GrantedProxyHandler(server.ProxyHandler):
            policy = server.AddressPolicy(
                public_ports=(80, 443),
                private_origins=(f"http://127.0.0.1:{origin_port}",),
                private_cidrs=("127.0.0.1/32",),
            )

        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            GrantedProxyHandler,
        )
        proxy_port = int(proxy.server_address[1])
        origin_thread = threading.Thread(
            target=origin.serve_forever,
            daemon=True,
        )
        proxy_thread = threading.Thread(
            target=proxy.serve_forever,
            daemon=True,
        )
        origin_thread.start()
        proxy_thread.start()
        try:
            with socket.create_connection(
                ("127.0.0.1", proxy_port),
                timeout=3,
            ) as client:
                client.sendall(
                    (
                        f"GET http://127.0.0.1:{origin_port}/proof HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{origin_port}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                response = bytearray()
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
            self.assertIn(b"HTTP/1.0 200 OK", response)
            self.assertTrue(response.endswith(b"deterministic-origin"))
        finally:
            proxy.shutdown()
            origin.shutdown()
            proxy.server_close()
            origin.server_close()
            proxy_thread.join(timeout=3)
            origin_thread.join(timeout=3)

    def test_unix_listener_is_private_and_healthcheck_uses_same_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            socket_path = parent / "proxy.sock"
            proxy = server.ThreadingUnixProxyServer(
                str(socket_path),
                server.ProxyHandler,
            )
            socket_path.chmod(0o660)
            proxy_thread = threading.Thread(
                target=proxy.serve_forever,
                daemon=True,
            )
            proxy_thread.start()
            try:
                self.assertTrue(server.healthcheck(socket_path=str(socket_path)))
                self.assertEqual(
                    0o660,
                    stat.S_IMODE(socket_path.stat().st_mode),
                )
            finally:
                proxy.shutdown()
                proxy.server_close()
                proxy_thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
