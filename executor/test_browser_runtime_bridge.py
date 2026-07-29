from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import sys
import urllib.request
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "executor"))

from browser_runtime.chatds_browser_runtime.proxy_bridge import (
    BridgeConfigurationError,
    LoopbackProxyBridge,
    MAX_CONNECTIONS,
    ProxySocketAuthority,
    ProxyTrustAuthority,
    _canonical_origin,
    _relay,
    main,
)
from browser_runtime.chatds_browser_runtime import policy as runtime_policy
from browser_runtime.chatds_browser_runtime.policy import (
    load_proxy_environment,
)
from skill_egress_proxy import server as proxy_server


TEST_POLICY_TOKEN = "test-egress-policy-token-" + "x" * 40


class BrowserRuntimeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"SKILL_EGRESS_POLICY_TOKEN": TEST_POLICY_TOKEN},
        )
        self.environment.start()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.parent = root / "public"
        self.private = root / "private"
        self.parent.mkdir()
        self.private.mkdir()
        self.parent.chmod(0o2710)
        self.private.chmod(0o700)
        self.socket_path = self.parent / "proxy.sock"
        self.servers = []
        self.threads = []
        self.authorities = []
        self.certificate_authority = proxy_server.CertificateAuthority(
            public_directory=self.parent,
            private_directory=self.private,
        )
        self.authorities.append(self.certificate_authority)
        proxy_server.ProxyHandler.certificate_authority = (
            self.certificate_authority
        )
        proxy_server.ProxyHandler.trust_generation = (
            self.certificate_authority.generation_id
        )

    def tearDown(self):
        for server in reversed(self.servers):
            server.shutdown()
            server.server_close()
        for thread in reversed(self.threads):
            thread.join(timeout=3)
        for authority in reversed(self.authorities):
            authority.close()
        proxy_server.ProxyHandler.certificate_authority = None
        proxy_server.ProxyHandler.trust_generation = None
        self.temporary.cleanup()
        self.environment.stop()

    def _start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        self.threads.append(thread)
        return server

    def _start_proxy_and_bridge(
        self,
        handler,
        *,
        origins: tuple[str, ...],
        trust_generation: str | None = None,
    ):
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
                origin_allowlist=origins,
                egress_rules=tuple({
                    "methods": list(
                        proxy_server.EGRESS_METHOD_ORDER
                    ),
                    "url_prefix": origin + "/",
                } for origin in origins),
                private_origins=origins,
                policy_token=TEST_POLICY_TOKEN,
                trust_generation=(
                    self.certificate_authority.generation_id
                    if trust_generation is None
                    else trust_generation
                ),
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
        port = self._start_proxy_and_bridge(
            proxy_server.ProxyHandler,
            origins=("https://127.0.0.1:443",),
        )
        response = self._request(
            port,
            b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n",
        )
        self.assertTrue(
            response.startswith(b"HTTP/1.1 403 "),
            response,
        )
        self.assertIn(b"destination_address_not_public", response)

    def test_empty_origin_bridge_is_authenticated_deny_all(self):
        port = self._start_proxy_and_bridge(
            proxy_server.ProxyHandler,
            origins=(),
        )
        response = self._request(
            port,
            b"CONNECT example.com:443 HTTP/1.1\r\n\r\n",
        )
        self.assertTrue(
            response.startswith(b"HTTP/1.1 403 "),
            response,
        )
        self.assertIn(b"destination_origin_not_allowed", response)

    def test_stale_execution_generation_is_rejected_by_proxy(self):
        port = self._start_proxy_and_bridge(
            proxy_server.ProxyHandler,
            origins=("https://example.com:443",),
            trust_generation="f" * 64,
        )
        # Exercise the scheduling window where the proxy rejects the signed
        # preface while the browser's CONNECT is already queued at the bridge.
        # Before the half-close/drain contract this returned an empty response
        # frequently, even though the proxy had generated a typed 403.
        for attempt in range(64):
            with self.subTest(attempt=attempt):
                response = self._request(
                    port,
                    b"CONNECT example.com:443 HTTP/1.1\r\n\r\n",
                )
                self.assertTrue(
                    response.startswith(b"HTTP/1.1 403 "),
                    response,
                )
                self.assertIn(
                    b"policy_trust_generation_mismatch",
                    response,
                )

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

        port = self._start_proxy_and_bridge(
            GrantedProxyHandler,
            origins=(f"http://127.0.0.1:{origin_port}",),
        )
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
                trust_generation=(
                    self.certificate_authority.generation_id
                ),
            )
        self.assertEqual(64, main(["--target", "example.com"]))

    def test_bridge_independently_validates_exact_policy_projection(self):
        authority = ProxySocketAuthority(
            self.socket_path,
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )
        origin = "https://example.com:443"
        invalid_policies = (
            {
                "origin_allowlist": (origin,),
                "egress_rules": (),
                "private_origins": (),
            },
            {
                "origin_allowlist": (origin,),
                "egress_rules": ({
                    "methods": ["HEAD", "GET"],
                    "url_prefix": origin + "/",
                },),
                "private_origins": (),
            },
            {
                "origin_allowlist": (origin,),
                "egress_rules": ({
                    "methods": ["GET"],
                    "url_prefix": origin + "/",
                },),
                "private_origins": (
                    "https://other.example:443",
                ),
            },
            {
                "origin_allowlist": (origin,),
                "egress_rules": ({
                    "methods": ["GET"],
                    "url_prefix": origin + "/api/../secret",
                },),
                "private_origins": (),
            },
        )
        for policy in invalid_policies:
            with (
                self.subTest(policy=policy),
                self.assertRaises(BridgeConfigurationError),
            ):
                LoopbackProxyBridge(
                    authority,
                    ("127.0.0.1", 0),
                    trust_generation=(
                        self.certificate_authority.generation_id
                    ),
                    **policy,
                )

    def test_one_execution_has_fixed_eight_connection_slots(self):
        self.assertEqual(8, MAX_CONNECTIONS)
        bridge = LoopbackProxyBridge(
            ProxySocketAuthority(
                self.socket_path,
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
            ),
            ("127.0.0.1", 0),
            trust_generation=self.certificate_authority.generation_id,
        )
        request, peer = socket.socketpair()
        try:
            for _index in range(MAX_CONNECTIONS):
                self.assertTrue(
                    bridge.verify_request(
                        request,
                        ("127.0.0.1", 1),
                    )
                )
            self.assertFalse(
                bridge.verify_request(
                    request,
                    ("127.0.0.1", 1),
                )
            )
            for _index in range(MAX_CONNECTIONS):
                bridge._admission.release()
        finally:
            request.close()
            peer.close()
            bridge.server_close()

    def test_bridge_rejects_wildcard_and_scoped_ipv6_origins(self):
        for origin in (
            "https://*.example.com:443",
            "https://api.*.example.com:443",
            "https://[fe80::1%25eth0]:443",
            "https://example.com:0",
        ):
            with self.subTest(origin=origin), self.assertRaises(
                BridgeConfigurationError,
            ):
                _canonical_origin(origin)

    def test_controller_copies_only_public_proxy_trust_per_execution(self):
        proxy = self._start(
            proxy_server.ThreadingUnixProxyServer(
                str(self.socket_path),
                proxy_server.ProxyHandler,
            )
        )
        self.socket_path.chmod(0o660)
        try:
            trust = ProxyTrustAuthority(
                self.parent / "ca.pem",
                self.parent / "leaf.spki",
                self.parent / "generation.json",
                expected_uid=os.geteuid(),
                expected_gid=os.getegid(),
            )
            with tempfile.TemporaryDirectory() as runtime_text:
                runtime_root = Path(runtime_text).resolve()
                runtime_root.chmod(0o700)
                environment = trust.materialize(
                    runtime_root,
                    worker_uid=os.geteuid(),
                    worker_gid=os.getegid(),
                )
                trust_root = runtime_root / ".chatds-egress-trust"
                self.assertEqual(
                    0o550,
                    trust_root.stat().st_mode & 0o777,
                )
                self.assertEqual(
                    0o440,
                    (trust_root / "ca.pem").stat().st_mode & 0o777,
                )
                self.assertEqual(
                    0o440,
                    (trust_root / "leaf.spki").stat().st_mode & 0o777,
                )
                self.assertEqual(
                    0o440,
                    (trust_root / "generation.json").stat().st_mode & 0o777,
                )
                self.assertEqual(
                    str(trust_root / "ca.pem"),
                    environment["SSL_CERT_FILE"],
                )
                self.assertEqual(
                    str(trust_root / "leaf.spki"),
                    environment["SKILL_EGRESS_LEAF_SPKI_PATH"],
                )
                with patch.object(
                    runtime_policy,
                    "_CONTROLLER_UID",
                    os.geteuid(),
                ):
                    loaded = load_proxy_environment({
                        **environment,
                        "SKILL_EGRESS_PROXY_URL": (
                            "http://127.0.0.1:18080"
                        ),
                    })
                self.assertEqual(
                    self.certificate_authority.leaf_spki,
                    loaded.leaf_spki_sha256,
                )
                self.assertEqual(
                    self.certificate_authority.generation_id,
                    loaded.trust_generation,
                )
                self.assertFalse(any(
                    path.suffix == ".key"
                    for path in self.parent.iterdir()
                ))
        finally:
            proxy.shutdown()

    def test_python_requests_curl_and_node_use_exact_https_proxy(self):
        try:
            import requests
        except ImportError:
            self.skipTest("requests is not installed in the test environment")

        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"client-compatibility-proof"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        class TlsOrigin(ThreadingHTTPServer):
            def get_request(self):
                raw, address = super().get_request()
                return (
                    self.tls_context.wrap_socket(
                        raw,
                        server_side=True,
                    ),
                    address,
                )

        certificate_authority = self.certificate_authority
        origin = TlsOrigin(("127.0.0.1", 0), OriginHandler)
        origin.tls_context = certificate_authority.server_context(
            "127.0.0.1"
        )
        self._start(origin)
        origin_port = int(origin.server_address[1])
        exact_origin = proxy_server.Origin(
            "https",
            "127.0.0.1",
            origin_port,
        )
        origin_text = proxy_server._origin_text(exact_origin)
        http_origin = self._start(
            ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
        )
        http_origin_text = (
            f"http://127.0.0.1:{int(http_origin.server_address[1])}"
        )
        ca = certificate_authority

        class GrantedProxyHandler(proxy_server.ProxyHandler):
            policy = proxy_server.AddressPolicy(
                public_ports=(80, 443),
                private_origins=(origin_text, http_origin_text),
                private_cidrs=("127.0.0.1/32",),
            )
            certificate_authority = ca
            upstream_tls_policy = proxy_server.UpstreamTlsPolicy({
                exact_origin: frozenset({
                    ca.leaf_spki,
                }),
            })

        bridge_port = self._start_proxy_and_bridge(
            GrantedProxyHandler,
            origins=(origin_text, http_origin_text),
        )
        proxy_url = f"http://127.0.0.1:{bridge_port}"
        ca_path = str(certificate_authority.public_ca_path)
        expected = "client-compatibility-proof"

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy_url}),
            urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=ca_path),
            ),
        )
        with patch.dict(
            os.environ,
            {"NO_PROXY": "", "no_proxy": ""},
            clear=False,
        ):
            with opener.open(origin_text + "/urllib", timeout=5) as result:
                self.assertEqual(
                    expected,
                    result.read().decode("ascii"),
                )

        session = requests.Session()
        session.trust_env = False
        result = session.get(
            origin_text + "/requests",
            proxies={"https": proxy_url},
            verify=ca_path,
            timeout=5,
        )
        self.assertEqual(expected, result.text)

        curl = shutil.which("curl")
        if curl is not None:
            completed = subprocess.run(
                [
                    curl,
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--noproxy",
                    "",
                    "--proxy",
                    proxy_url,
                    "--cacert",
                    ca_path,
                    origin_text + "/curl",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            self.assertEqual(expected, completed.stdout)

        node = shutil.which("node")
        if node is not None:
            version = subprocess.run(
                [node, "-p", "process.versions.node"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                timeout=5,
            ).stdout.strip()
            if tuple(int(item) for item in version.split(".")[:2]) >= (
                22,
                21,
            ):
                environment = {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HTTP_PROXY": proxy_url,
                    "HTTPS_PROXY": proxy_url,
                    "NO_PROXY": "",
                    "NODE_USE_ENV_PROXY": "1",
                    "NODE_EXTRA_CA_CERTS": ca_path,
                }
                scripts = {
                    "fetch-https": (
                        f"fetch({origin_text!r} + '/node-fetch')"
                        ".then(async r => {"
                        " if (!r.ok) throw new Error(String(r.status));"
                        " process.stdout.write(await r.text());"
                        "})"
                        ".catch(e => { console.error(e); process.exit(2); });"
                    ),
                    "fetch-http-connect": (
                        f"fetch({http_origin_text!r} + '/node-fetch-http')"
                        ".then(async r => {"
                        " if (!r.ok) throw new Error(String(r.status));"
                        " process.stdout.write(await r.text());"
                        "})"
                        ".catch(e => { console.error(e); process.exit(2); });"
                    ),
                    "core-http": (
                        "require('node:http').get("
                        f"{http_origin_text!r} + '/node-core-http', r => {{"
                        " let body = '';"
                        " r.on('data', chunk => body += chunk);"
                        " r.on('end', () => process.stdout.write(body));"
                        "}).on('error', e => {"
                        " console.error(e); process.exit(2);"
                        "});"
                    ),
                    "core-https": (
                        "require('node:https').get("
                        f"{origin_text!r} + '/node-core-https', r => {{"
                        " let body = '';"
                        " r.on('data', chunk => body += chunk);"
                        " r.on('end', () => process.stdout.write(body));"
                        "}).on('error', e => {"
                        " console.error(e); process.exit(2);"
                        "});"
                    ),
                }
                for name, script in scripts.items():
                    with self.subTest(node_client=name):
                        completed = subprocess.run(
                            [node, "-e", script],
                            check=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            env=environment,
                            timeout=10,
                        )
                        self.assertEqual(expected, completed.stdout)


if __name__ == "__main__":
    unittest.main()
