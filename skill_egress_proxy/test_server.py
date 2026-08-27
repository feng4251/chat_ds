from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import socketserver
import ssl
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from skill_egress_proxy import server
from claude_runner.config import DEFAULT_CLAUDE_EGRESS_LIMITS


TEST_POLICY_TOKEN = "test-egress-policy-token-" + "x" * 40
TEST_TRUST_GENERATION = "a" * 64


def _policy_preface(
    origins: list[str],
    *,
    version: int | float | bool = 2,
    egress_rules: list[dict[str, object]] | None = None,
    private_origins: list[str] | None = None,
    expires_unix: int | None = None,
    nonce: str = "1" * 32,
    token: str = TEST_POLICY_TOKEN,
    trust_generation: str | None = None,
    auth_hmac: str | None = None,
    budget_scope_sha256: str = "b" * 64,
    call_id_sha256: str = "c" * 64,
    limits: dict[str, object] | None = None,
    public_read: dict[str, object] | None = None,
) -> bytes:
    if trust_generation is None:
        trust_generation = (
            server.ProxyHandler.trust_generation
            or TEST_TRUST_GENERATION
        )
    if egress_rules is None:
        egress_rules = [
            {
                "methods": list(server.EGRESS_METHOD_ORDER),
                "url_prefix": origin + "/",
            }
            for origin in origins
        ]
    if private_origins is None:
        private_origins = list(origins)
    unsigned = {
        "version": version,
        "expires_unix": (
            int(time.time()) + 60
            if expires_unix is None
            else expires_unix
        ),
        "nonce": nonce,
        "origins": origins,
        "egress_rules": egress_rules,
        "private_origins": private_origins,
        "trust_generation": trust_generation,
    }
    if version == 3:
        unsigned.update({
            "public_read": public_read,
            "budget_scope_sha256": budget_scope_sha256,
            "call_id_sha256": call_id_sha256,
            "limits": (
                {
                    "max_requests": 2_048,
                    "max_outbound_bytes": 16 * 1024 * 1024,
                    "max_response_wire_bytes": 512 * 1024 * 1024,
                }
                if limits is None
                else limits
            ),
        })
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = {
        **unsigned,
        "auth_hmac": (
            hmac.new(
                hmac.new(
                    token.encode(),
                    server.POLICY_KEY_DERIVATION_LABEL,
                    hashlib.sha256,
                ).digest(),
                canonical,
                hashlib.sha256,
            ).hexdigest()
            if auth_hmac is None
            else auth_hmac
        ),
    }
    return server.POLICY_PREFACE_PREFIX + json.dumps(
        payload,
        separators=(",", ":"),
    ).encode() + b"\n"


class _FragmentedReader:
    def __init__(self, payload: bytes, *, max_chunk: int = 1) -> None:
        self.payload = bytearray(payload)
        self.max_chunk = max_chunk
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, size: int) -> bytes:
        if not self.payload:
            return b""
        count = min(size, self.max_chunk, len(self.payload))
        result = bytes(self.payload[:count])
        del self.payload[:count]
        return result


class _DelayedReader:
    def __init__(self, clock: list[float], payload: bytes) -> None:
        self.clock = clock
        self.payload = payload
        self.delayed = False

    def settimeout(self, _value: float) -> None:
        return

    def recv(self, _size: int) -> bytes:
        if not self.delayed:
            self.delayed = True
            self.clock[0] += 31.0
            raise socket.timeout
        payload, self.payload = self.payload, b""
        return payload


class _CollectingWriter:
    def __init__(self) -> None:
        self.payload = bytearray()
        self.timeouts: list[float] = []
        self.maximum_write = 0

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def sendall(self, content: bytes) -> None:
        self.maximum_write = max(self.maximum_write, len(content))
        self.payload.extend(content)


class _RepeatingReader:
    def __init__(self, remaining: int) -> None:
        self.remaining = remaining
        self.maximum_read = 0
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, size: int) -> bytes:
        self.maximum_read = max(self.maximum_read, size)
        if not self.remaining:
            return b""
        count = min(size, self.remaining)
        self.remaining -= count
        return b"x" * count


class _DiscardingWriter:
    def __init__(self) -> None:
        self.total = 0
        self.maximum_write = 0
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def sendall(self, content: bytes) -> None:
        self.total += len(content)
        self.maximum_write = max(self.maximum_write, len(content))


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
        self.assertIn(b"Host: example.com\r\n", forwarded)
        self.assertIn(b"Connection: close\r\n", forwarded)

    def test_credentials_and_non_absolute_http_are_rejected(self):
        with self.assertRaises(server.ProxyPolicyError):
            server._request_destination(
                b"GET /relative HTTP/1.1\r\nHost: example.com\r\n\r\n"
            )
        with self.assertRaises(server.ProxyPolicyError):
            server._request_destination(
                b"GET http://user:password@example.com/ HTTP/1.1\r\n\r\n"
            )

    def test_http_host_must_match_target_and_be_unique(self):
        for request, reason in (
            (
                b"GET http://allowed.example/a HTTP/1.1\r\n"
                b"Host: other.example\r\n\r\n",
                "http_host_target_mismatch",
            ),
            (
                b"GET http://allowed.example/a HTTP/1.1\r\n\r\n",
                "http_host_header_required",
            ),
            (
                b"GET http://allowed.example/a HTTP/1.1\r\n"
                b"Host: allowed.example\r\n"
                b"Host: allowed.example\r\n\r\n",
                "duplicate_http_host_header",
            ),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(
                server.ProxyPolicyError,
                reason,
            ):
                server._request_destination(request)

    def test_equivalent_http_host_is_rewritten_to_canonical_target(self):
        scheme, host, port, forwarded = server._request_destination(
            b"GET http://EXAMPLE.com.:080/proof HTTP/1.1\r\n"
            b"Host: EXAMPLE.com.:80\r\n"
            b"Connection: keep-alive\r\n\r\n"
        )
        self.assertEqual(
            ("http", "example.com", 80),
            (scheme, host, port),
        )
        self.assertEqual(1, forwarded.count(b"Host: example.com\r\n"))
        self.assertNotIn(b"Host: EXAMPLE.com.", forwarded)
        self.assertEqual(1, forwarded.count(b"Connection: close\r\n"))
        self.assertNotIn(b"keep-alive", forwarded)

    def test_zero_port_and_ambiguous_host_authorities_are_rejected(self):
        for request in (
            (
                b"GET http://example.com:0/ HTTP/1.1\r\n"
                b"Host: example.com:0\r\n\r\n"
            ),
            (
                b"GET http://example.com/ HTTP/1.1\r\n"
                b"Host: user@example.com\r\n\r\n"
            ),
            (
                b"GET http://example.com/ HTTP/1.1\r\n"
                b"Host: example.com,other.example\r\n\r\n"
            ),
        ):
            with self.subTest(request=request), self.assertRaises(
                server.ProxyPolicyError,
            ):
                server._request_destination(request)
        with self.assertRaises(server.ProxyPolicyError):
            server._origin_tuple("https://example.com:0")

    def test_forwarding_identity_headers_are_stripped_in_both_lanes(self):
        headers = (
            b"Forwarded: for=192.0.2.1\r\n"
            b"X-Forwarded-For: 192.0.2.1\r\n"
            b"X-Forwarded-Proto: https\r\n"
            b"X-Real-IP: 192.0.2.1\r\n"
            b"Via: 1.1 attacker\r\n"
            b"X-Benign: retained\r\n"
        )
        _scheme, _host, _port, absolute = server._request_destination(
            b"GET http://example.com/a HTTP/1.1\r\n"
            b"Host: example.com\r\n" + headers + b"\r\n"
        )
        tunneled = server._tunneled_origin_request(
            b"GET /a HTTP/1.1\r\n"
            b"Host: example.com\r\n" + headers + b"\r\n",
            server.Origin("https", "example.com", 443),
        )
        for forwarded in (absolute, tunneled):
            self.assertNotIn(b"Forwarded:", forwarded)
            self.assertNotIn(b"X-Forwarded", forwarded)
            self.assertNotIn(b"X-Real-IP:", forwarded)
            self.assertNotIn(b"Via:", forwarded)
            self.assertIn(b"X-Benign: retained", forwarded)

    def test_request_target_and_header_metadata_limits(self):
        allowed_headers = (
            b"GET http://example.com/ HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            + b"".join(
                b"X-A: x\r\n"
                for _ in range(
                    server.MAX_HTTP_REQUEST_HEADER_FIELDS - 1
                )
            )
            + b"\r\n"
        )
        server._request_destination(allowed_headers)
        target = b"/" + b"a" * server.MAX_HTTP_REQUEST_TARGET_BYTES
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "http_request_metadata_too_large",
        ):
            server._tunneled_origin_request(
                b"GET " + target + b" HTTP/1.1\r\n"
                b"Host: example.com\r\n\r\n",
                server.Origin("https", "example.com", 443),
            )
        excessive_headers = (
            b"GET http://example.com/ HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            + b"".join(
                b"X-A: x\r\n"
                for _ in range(server.MAX_HTTP_REQUEST_HEADER_FIELDS)
            )
            + b"\r\n"
        )
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "http_request_metadata_too_large",
        ):
            server._request_destination(excessive_headers)

    def test_header_name_and_value_limits(self):
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "invalid_request_header",
        ):
            server._validated_header_parts(
                b"a" * (server.MAX_HTTP_REQUEST_HEADER_NAME_BYTES + 1)
                + b": x"
            )
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "invalid_request_header",
        ):
            server._validated_header_parts(
                b"X-A:"
                + b"x"
                * (server.MAX_HTTP_REQUEST_HEADER_VALUE_BYTES + 1)
            )


class RequestQueryBoundaryTests(unittest.TestCase):
    def test_query_count_key_and_value_boundaries(self):
        origin = server.Origin("https", "example.com", 443)
        exact_count = "&".join(
            f"k{i}=v"
            for i in range(server.MAX_HTTP_QUERY_FIELDS)
        )
        self.assertEqual(
            exact_count,
            server._request_policy_coordinate(
                (
                    f"GET /?{exact_count} HTTP/1.1\r\n"
                    "Host: example.com\r\n\r\n"
                ).encode(),
                origin,
            )[2],
        )
        too_many = "&".join(
            f"k{i}=v"
            for i in range(server.MAX_HTTP_QUERY_FIELDS + 1)
        )
        too_long_key = "k" * (server.MAX_HTTP_QUERY_KEY_BYTES + 1)
        too_long_value = "v" * (
            server.MAX_HTTP_QUERY_VALUE_BYTES + 1
        )
        for query in (
            too_many,
            f"{too_long_key}=v",
            f"k={too_long_value}",
        ):
            forwarded = (
                f"GET /?{query} HTTP/1.1\r\n"
                "Host: example.com\r\n\r\n"
            ).encode()
            with self.subTest(query_length=len(query)), (
                self.assertRaises(server.ProxyPolicyError)
            ):
                server._request_policy_coordinate(
                    forwarded,
                    origin,
                )


class HttpSingleRequestFramingTests(unittest.TestCase):
    def test_content_length_body_is_streamed_without_second_request(self):
        _scheme, _host, _port, forwarded = server._request_destination(
            b"POST http://example.com/upload HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Content-Length: 5\r\n\r\nhe"
        )
        upstream = _CollectingWriter()
        server._forward_single_http_request(
            _FragmentedReader(b"llo", max_chunk=1),
            upstream,
            forwarded,
        )
        self.assertTrue(upstream.payload.endswith(b"\r\n\r\nhello"))

    def test_get_and_head_request_bodies_are_rejected(self):
        for method in ("GET", "HEAD"):
            _scheme, _host, _port, forwarded = (
                server._request_destination(
                    (
                        f"{method} http://example.com/data HTTP/1.1\r\n"
                        "Host: example.com\r\n"
                        "Content-Length: 4\r\n\r\ndata"
                    ).encode("ascii")
                )
            )
            with self.subTest(method=method), self.assertRaisesRegex(
                server.ProxyPolicyError,
                "read_only_http_method_body_not_allowed",
            ):
                server._validated_request_body_framing(forwarded)

    def test_chunked_body_is_rejected_before_upstream_write(self):
        _scheme, _host, _port, forwarded = server._request_destination(
            b"POST http://example.com/upload HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n4\r\nWi"
        )
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "unsupported_http_transfer_encoding",
        ):
            server._validated_request_body_framing(forwarded)

    def test_pipelined_or_ambiguous_request_framing_is_rejected(self):
        requests = (
            (
                b"GET http://example.com/ HTTP/1.1\r\n"
                b"Host: example.com\r\n\r\n"
                b"GET /other HTTP/1.1\r\nHost: other.example\r\n\r\n"
            ),
            (
                b"POST http://example.com/ HTTP/1.1\r\n"
                b"Host: example.com\r\n"
                b"Content-Length: 0\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            ),
        )
        for request in requests:
            _scheme, _host, _port, forwarded = (
                server._request_destination(request)
            )
            with self.subTest(request=request), self.assertRaises(
                server.ProxyPolicyError,
            ):
                server._validated_request_body_framing(forwarded)

    def test_maximum_body_streams_in_fixed_chunks_without_accumulation(
        self,
    ):
        body_size = server.MAX_HTTP_REQUEST_BODY_BYTES
        forwarded = (
            b"POST /upload HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            + f"Content-Length: {body_size}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
        )
        reader = _RepeatingReader(body_size)
        upstream = _DiscardingWriter()
        server._forward_single_http_request(
            reader,
            upstream,
            forwarded,
        )
        header_size = len(forwarded)
        self.assertEqual(header_size + body_size, upstream.total)
        self.assertLessEqual(
            reader.maximum_read,
            server.COPY_CHUNK_BYTES,
        )
        self.assertLessEqual(
            upstream.maximum_write,
            server.MAX_HEADER_BYTES,
        )

    def test_legal_slow_body_does_not_inherit_header_write_deadline(
        self,
    ):
        clock = [0.0]

        class SlowReader:
            def __init__(self) -> None:
                self.remaining = bytearray(b"body")

            def settimeout(self, _value: float) -> None:
                return

            def recv(self, _size: int) -> bytes:
                clock[0] += 9.0
                if not self.remaining:
                    return b""
                value = bytes(self.remaining[:1])
                del self.remaining[:1]
                return value

        forwarded = (
            b"POST /upload HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Content-Length: 4\r\n"
            b"Connection: close\r\n\r\n"
        )
        upstream = _CollectingWriter()
        with patch.object(
            server.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ):
            server._forward_single_http_request(
                SlowReader(),
                upstream,
                forwarded,
            )
        self.assertTrue(upstream.payload.endswith(b"\r\n\r\nbody"))
        self.assertGreater(
            clock[0],
            server.UPSTREAM_REQUEST_WRITE_TIMEOUT_SECONDS,
        )

    def test_expired_upstream_write_deadline_is_gateway_timeout(self):
        with (
            patch.object(server.time, "monotonic", return_value=2.0),
            self.assertRaisesRegex(
                server.ProxyTransportTimeoutError,
                "upstream_request_write_timeout",
            ) as raised,
        ):
            server._send_request_part(
                _CollectingWriter(),
                b"request",
                absolute_deadline=1.0,
            )
        self.assertEqual(
            504,
            server._transport_error_status(raised.exception),
        )


class PolicyPrefaceAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"SKILL_EGRESS_POLICY_TOKEN": TEST_POLICY_TOKEN},
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    @staticmethod
    def _read_preface(payload: bytes):
        receiving, sending = socket.socketpair()
        try:
            sending.sendall(payload)
            sending.shutdown(socket.SHUT_WR)
            return server._read_policy_preface(
                receiving,
                expected_trust_generation=TEST_TRUST_GENERATION,
            )
        finally:
            receiving.close()
            sending.close()

    def test_valid_preface_returns_only_signed_origins_and_remainder(self):
        policy, remainder = self._read_preface(
            _policy_preface(["https://example.com:443"])
            + b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
        )
        self.assertEqual(
            frozenset({
                server.Origin("https", "example.com", 443),
            }),
            policy.origins,
        )
        self.assertEqual(1, len(policy.rules))
        self.assertEqual(policy.origins, policy.private_origins)
        self.assertEqual(
            b"CONNECT example.com:443 HTTP/1.1\r\n\r\n",
            remainder,
        )

    def test_deployment_can_require_v3_for_nonempty_egress_authority(self):
        with (
            patch.object(server, "REQUIRE_POLICY_V3", True),
            self.assertRaisesRegex(
                server.ProxyPolicyError,
                "egress_policy_upgrade_required",
            ),
        ):
            self._read_preface(
                _policy_preface(["https://example.com:443"])
            )

        with patch.object(server, "REQUIRE_POLICY_V3", True):
            policy, remainder = self._read_preface(
                _policy_preface(
                    [],
                    egress_rules=[],
                    private_origins=[],
                )
            )
        self.assertEqual(2, policy.version)
        self.assertEqual((), policy.rules)
        self.assertEqual(b"", remainder)

    def test_bad_hmac_is_rejected(self):
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "policy_authentication_failed",
        ):
            self._read_preface(
                _policy_preface(
                    ["https://example.com:443"],
                    auth_hmac="0" * 64,
                )
            )

    def test_raw_or_cross_domain_key_cannot_authenticate_preface(self):
        unsigned = {
            "version": 2,
            "expires_unix": int(time.time()) + 60,
            "nonce": "2" * 32,
            "origins": ["https://example.com:443"],
            "egress_rules": [{
                "methods": list(server.EGRESS_METHOD_ORDER),
                "url_prefix": "https://example.com:443/",
            }],
            "private_origins": ["https://example.com:443"],
            "trust_generation": TEST_TRUST_GENERATION,
        }
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        for signing_key in (
            TEST_POLICY_TOKEN.encode(),
            hmac.new(
                TEST_POLICY_TOKEN.encode(),
                b"chatds-other-protocol-v1",
                hashlib.sha256,
            ).digest(),
        ):
            payload = {
                **unsigned,
                "auth_hmac": hmac.new(
                    signing_key,
                    canonical,
                    hashlib.sha256,
                ).hexdigest(),
            }
            with self.subTest(signing_key=signing_key), (
                self.assertRaisesRegex(
                    server.ProxyPolicyError,
                    "policy_authentication_failed",
                )
            ):
                self._read_preface(
                    server.POLICY_PREFACE_PREFIX
                    + json.dumps(payload, separators=(",", ":")).encode()
                    + b"\n"
                )

    def test_generation_mismatch_is_typed(self):
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "policy_trust_generation_mismatch",
        ):
            self._read_preface(
                _policy_preface(
                    ["https://example.com:443"],
                    trust_generation="b" * 64,
                )
            )

    def test_preface_and_headers_have_absolute_idle_deadlines(self):
        receiving, sending = socket.socketpair()
        try:
            with (
                patch.object(
                    server,
                    "POLICY_PREFACE_ABSOLUTE_READ_TIMEOUT_SECONDS",
                    0.05,
                ),
                patch.object(
                    server,
                    "HTTP_READ_IDLE_TIMEOUT_SECONDS",
                    0.02,
                ),
                self.assertRaisesRegex(
                    server.ProxyPolicyError,
                    "policy_preface_read_timeout",
                ),
            ):
                server._read_policy_preface(
                    receiving,
                    expected_trust_generation=TEST_TRUST_GENERATION,
                )
        finally:
            receiving.close()
            sending.close()

        receiving, sending = socket.socketpair()
        try:
            with (
                patch.object(
                    server,
                    "HTTP_HEADER_ABSOLUTE_READ_TIMEOUT_SECONDS",
                    0.05,
                ),
                patch.object(
                    server,
                    "HTTP_READ_IDLE_TIMEOUT_SECONDS",
                    0.02,
                ),
                self.assertRaisesRegex(
                    server.ProxyPolicyError,
                    "request_headers_read_timeout",
                ),
            ):
                server._read_headers(
                    receiving,
                    initial=b"GET / HTTP/1.1\r\n",
                )
        finally:
            receiving.close()
            sending.close()

    def test_expired_and_far_future_prefaces_are_rejected(self):
        now = int(time.time())
        for expires_unix in (
            now - server.POLICY_CLOCK_SKEW_SECONDS - 1,
            now
            + server.MAX_POLICY_TTL_SECONDS
            + server.POLICY_CLOCK_SKEW_SECONDS
            + 1,
        ):
            with self.subTest(expires_unix=expires_unix), self.assertRaisesRegex(
                server.ProxyPolicyError,
                "invalid_policy_preface",
            ):
                self._read_preface(
                    _policy_preface(
                        ["https://example.com:443"],
                        expires_unix=expires_unix,
                    )
                )

    def test_non_integer_policy_version_is_rejected(self):
        for version in (True, 1.0, 1):
            with self.subTest(version=version), self.assertRaisesRegex(
                server.ProxyPolicyError,
                "invalid_policy_preface",
            ):
                self._read_preface(
                    _policy_preface(
                        ["https://example.com:443"],
                        version=version,
                    )
                )

    def test_valid_v3_preface_authenticates_budget_metadata(self):
        policy, remainder = self._read_preface(
            _policy_preface(
                ["https://example.com:443"],
                version=3,
                limits={
                    "max_requests": 7,
                    "max_outbound_bytes": 8_000,
                    "max_response_wire_bytes": 90_000,
                },
            )
        )
        self.assertEqual(b"", remainder)
        self.assertEqual(3, policy.version)
        self.assertEqual("b" * 64, policy.budget_scope_sha256)
        self.assertEqual("c" * 64, policy.call_id_sha256)
        self.assertEqual(
            server.PolicyBudgetLimits(7, 8_000, 90_000),
            policy.limits,
        )

    def test_v3_requires_exact_bounded_budget_fields(self):
        cases = (
            {"budget_scope_sha256": "B" * 64},
            {"call_id_sha256": "short"},
            {"limits": {"max_requests": 1}},
            {
                "limits": {
                    "max_requests": server.MAX_REQUESTS_PER_SCOPE + 1,
                    "max_outbound_bytes": 1,
                    "max_response_wire_bytes": 1,
                },
            },
            {
                "limits": {
                    "max_requests": True,
                    "max_outbound_bytes": 1,
                    "max_response_wire_bytes": 1,
                },
            },
        )
        for override in cases:
            kwargs = {
                "budget_scope_sha256": "b" * 64,
                "call_id_sha256": "c" * 64,
                "limits": {
                    "max_requests": 1,
                    "max_outbound_bytes": 1,
                    "max_response_wire_bytes": 1,
                },
                **override,
            }
            with self.subTest(override=override), self.assertRaisesRegex(
                server.ProxyPolicyError,
                "invalid_policy_preface",
            ):
                self._read_preface(
                    _policy_preface(
                        ["https://example.com:443"],
                        version=3,
                        **kwargs,
                    )
                )

    def test_v3_budget_metadata_is_hmac_covered(self):
        valid = _policy_preface(
            ["https://example.com:443"],
            version=3,
            limits={
                "max_requests": 3,
                "max_outbound_bytes": 100,
                "max_response_wire_bytes": 200,
            },
        )
        encoded = valid[len(server.POLICY_PREFACE_PREFIX):].strip()
        original = json.loads(encoded)
        mutations = (
            ("budget_scope_sha256", "d" * 64),
            ("call_id_sha256", "e" * 64),
            (
                "limits",
                {
                    "max_requests": 2,
                    "max_outbound_bytes": 100,
                    "max_response_wire_bytes": 200,
                },
            ),
        )
        for field, value in mutations:
            payload = {**original, field: value}
            tampered = (
                server.POLICY_PREFACE_PREFIX
                + json.dumps(payload, separators=(",", ":")).encode()
                + b"\n"
            )
            with self.subTest(field=field), self.assertRaisesRegex(
                server.ProxyPolicyError,
                "policy_authentication_failed",
            ):
                self._read_preface(tampered)

    def test_raw_unsigned_request_is_rejected(self):
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "authenticated_policy_preface_required",
        ):
            self._read_preface(
                b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
            )

    def test_signed_wildcard_origin_is_rejected(self):
        for origin in (
            "https://*.example.com:443",
            "https://api.*.example.com:443",
        ):
            with self.subTest(origin=origin), self.assertRaises(
                server.ProxyPolicyError,
            ):
                self._read_preface(_policy_preface([origin]))


class PolicyScopeLedgerTests(unittest.TestCase):
    @staticmethod
    def _policy(
        scope: str,
        *,
        requests: int = 2,
        outbound: int = 10,
        response: int = 10,
    ) -> server.SignedEgressPolicy:
        return server._validated_signed_egress_policy(
            [],
            [],
            [],
            version=3,
            budget_scope_sha256=scope,
            call_id_sha256="c" * 64,
            limits_raw={
                "max_requests": requests,
                "max_outbound_bytes": outbound,
                "max_response_wire_bytes": response,
            },
        )

    def test_shared_scope_enforces_all_three_cumulative_limits(self):
        ledger = server.PolicyScopeLedger(capacity=2, ttl_seconds=60)
        policy = self._policy("a" * 64, requests=2)
        first = ledger.admit(policy)
        second = ledger.admit(policy)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        first.consume_outbound(6)
        second.consume_outbound(4)
        first.consume_response_wire(10)
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "policy_outbound_budget_exceeded",
        ):
            second.consume_outbound(1)
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "policy_response_wire_budget_exceeded",
        ):
            second.consume_response_wire(1)
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "policy_request_budget_exceeded",
        ):
            ledger.admit(policy)
        first.release()
        second.release()

    def test_proxy_ceiling_accepts_default_claude_turn_budget(self):
        policy = self._policy(
            "a" * 64,
            requests=DEFAULT_CLAUDE_EGRESS_LIMITS["max_requests"],
            outbound=DEFAULT_CLAUDE_EGRESS_LIMITS["max_outbound_bytes"],
            response=DEFAULT_CLAUDE_EGRESS_LIMITS[
                "max_response_wire_bytes"
            ],
        )
        self.assertEqual(
            policy.limits.max_requests,
            DEFAULT_CLAUDE_EGRESS_LIMITS["max_requests"],
        )

    def test_scope_limit_drift_and_capacity_fail_closed(self):
        ledger = server.PolicyScopeLedger(capacity=1, ttl_seconds=60)
        reservation = ledger.admit(self._policy("a" * 64))
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "policy_scope_limits_mismatch",
        ):
            ledger.admit(self._policy("a" * 64, outbound=9))
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "policy_scope_ledger_capacity_exceeded",
        ):
            ledger.admit(self._policy("b" * 64))
        reservation.release()

    def test_inactive_scope_expires_but_active_scope_does_not(self):
        now = [0.0]
        ledger = server.PolicyScopeLedger(
            capacity=1,
            ttl_seconds=5,
            clock=lambda: now[0],
        )
        active = ledger.admit(self._policy("a" * 64))
        now[0] = 10.0
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "policy_scope_ledger_capacity_exceeded",
        ):
            ledger.admit(self._policy("b" * 64))
        active.release()
        now[0] = 16.0
        replacement = ledger.admit(self._policy("b" * 64))
        replacement.release()

    def test_active_oldest_scope_does_not_block_expired_inactive_reclaim(self):
        now = [0.0]
        ledger = server.PolicyScopeLedger(
            capacity=2,
            ttl_seconds=5,
            clock=lambda: now[0],
        )
        active = ledger.admit(self._policy("a" * 64))
        inactive = ledger.admit(self._policy("b" * 64))
        inactive.release()
        now[0] = 10.0

        replacement = ledger.admit(self._policy("c" * 64))

        self.assertEqual(2, len(ledger._states))
        self.assertIn("a" * 64, ledger._states)
        self.assertIn("c" * 64, ledger._states)
        active.release()
        replacement.release()

    def test_concurrent_request_admission_is_atomic(self):
        ledger = server.PolicyScopeLedger(capacity=1, ttl_seconds=60)
        policy = self._policy("a" * 64, requests=8)
        barrier = threading.Barrier(16)
        outcomes: list[str] = []
        lock = threading.Lock()

        def admit() -> None:
            barrier.wait()
            try:
                reservation = ledger.admit(policy)
            except server.ProxyPolicyError as exc:
                result = str(exc)
            else:
                result = "admitted"
                reservation.release()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=admit) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(8, outcomes.count("admitted"))
        self.assertEqual(
            8,
            outcomes.count("policy_request_budget_exceeded"),
        )

    def test_response_relay_stops_before_over_budget_chunk(self):
        ledger = server.PolicyScopeLedger(capacity=1, ttl_seconds=60)
        reservation = ledger.admit(
            self._policy("a" * 64, response=5)
        )
        client = _CollectingWriter()
        upstream = _FragmentedReader(b"abcdef", max_chunk=1)
        server._relay_response_only(client, upstream, reservation)
        self.assertEqual(b"abcde", bytes(client.payload))
        reservation.release()

    def test_exact_provider_idle_budget_defers_to_native_stream_watchdog(self):
        short_clock = [0.0]
        short_client = _CollectingWriter()
        server._relay_response_only(
            short_client,
            _DelayedReader(short_clock, b"late-provider-byte"),
            idle_timeout_seconds=30,
            clock=lambda: short_clock[0],
        )
        self.assertEqual(bytes(short_client.payload), b"")

        native_clock = [0.0]
        native_client = _CollectingWriter()
        server._relay_response_only(
            native_client,
            _DelayedReader(native_clock, b"late-provider-byte"),
            idle_timeout_seconds=60,
            clock=lambda: native_clock[0],
        )
        self.assertEqual(
            bytes(native_client.payload),
            b"late-provider-byte",
        )


class ExactEgressPolicyTests(unittest.TestCase):
    def setUp(self):
        self.origin = server.Origin(
            "https",
            "example.com",
            443,
        )
        self.policy = server._validated_signed_egress_policy(
            ["https://example.com:443"],
            [
                {
                    "methods": ["GET", "HEAD"],
                    "url_prefix": (
                        "https://example.com:443/api/v1"
                    ),
                },
                {
                    "methods": ["GET"],
                    "url_prefix": (
                        "https://example.com:443/docs/"
                    ),
                },
                {
                    "methods": ["POST"],
                    "url_prefix": (
                        "https://example.com:443/search?"
                        "q=galectin"
                    ),
                },
                {
                    "methods": ["GET"],
                    "url_prefix": "https://example.com:443/exact?view=full",
                    "query_exact": True,
                },
            ],
            [],
        )

    @staticmethod
    def _request(method: str, target: str) -> bytes:
        return (
            f"{method} {target} HTTP/1.1\r\n"
            "Host: example.com\r\n\r\n"
        ).encode("ascii")

    def test_long_idle_budget_is_signed_exact_post_authority_only(self):
        policy = server._validated_signed_egress_policy(
            ["https://provider.example:443"],
            [{
                "methods": ["POST"],
                "url_prefix": (
                    "https://provider.example:443/v1/chat/completions"
                ),
                "query_exact": True,
                "response_idle_timeout_seconds": 7_260,
            }],
            [],
            version=3,
            budget_scope_sha256="a" * 64,
            call_id_sha256="b" * 64,
            limits_raw={
                "max_requests": 8,
                "max_outbound_bytes": 4096,
                "max_response_wire_bytes": 65_536,
            },
        )
        origin = server.Origin("https", "provider.example", 443)
        rule = server._authorize_exact_request(
            policy,
            origin,
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: provider.example\r\n\r\n",
        )
        self.assertEqual(rule.response_idle_timeout_seconds, 7_260)

        invalid_rules = (
            {
                "methods": ["GET"],
                "url_prefix": "https://provider.example:443/document",
                "query_exact": True,
                "response_idle_timeout_seconds": 7_260,
            },
            {
                "methods": ["POST"],
                "url_prefix": "https://provider.example:443/v1/",
                "response_idle_timeout_seconds": 7_260,
            },
        )
        for invalid in invalid_rules:
            with self.subTest(rule=invalid), self.assertRaises(
                server.ProxyPolicyError,
            ):
                server._validated_signed_egress_policy(
                    ["https://provider.example:443"],
                    [invalid],
                    [],
                    version=3,
                    budget_scope_sha256="a" * 64,
                    call_id_sha256="b" * 64,
                    limits_raw={
                        "max_requests": 8,
                        "max_outbound_bytes": 4096,
                        "max_response_wire_bytes": 65_536,
                    },
                )

    def test_path_boundary_method_and_query_prefix_are_exact(self):
        allowed = (
            ("GET", "/api/v1"),
            ("HEAD", "/api/v1?trace=1"),
            ("GET", "/docs/"),
            ("GET", "/docs/chapter"),
            ("POST", "/search?q=galectin-3"),
            ("POST", "/search?q=galectin&phase=2"),
            ("GET", "/exact?view=full"),
        )
        for method, target in allowed:
            with self.subTest(method=method, target=target):
                server._authorize_exact_request(
                    self.policy,
                    self.origin,
                    self._request(method, target),
                )

        denied = (
            ("GET", "/api/v10"),
            ("GET", "/api/v1/child"),
            ("POST", "/api/v1"),
            ("GET", "/docs"),
            ("DELETE", "/docs/chapter"),
            ("POST", "/search?x=1&q=galectin"),
            ("GET", "/search?q=galectin"),
            ("GET", "/exact"),
            ("GET", "/exact?view=full&secret=1"),
        )
        for method, target in denied:
            with (
                self.subTest(method=method, target=target),
                self.assertRaisesRegex(
                    server.ProxyPolicyError,
                    "request_url_not_allowed",
                ),
            ):
                server._authorize_exact_request(
                    self.policy,
                    self.origin,
                    self._request(method, target),
                )

    def test_request_target_ambiguities_fail_closed(self):
        for target in (
            "//example.com/api/v1",
            "/api//v1",
            "/api/../v1",
            "/api/%2Fv1",
            "/api/%25v1",
            "/api/%23v1",
            "/api/v1#fragment",
            "/api/v1?x=%0Aheader",
        ):
            with (
                self.subTest(target=target),
                self.assertRaises(server.ProxyPolicyError),
            ):
                server._authorize_exact_request(
                    self.policy,
                    self.origin,
                    self._request("GET", target),
                )

    def test_printable_percent_encoded_query_data_is_not_path_traversal(self):
        policy = server._validated_signed_egress_policy(
            ["https://example.com:443"],
            [{
                "methods": ["GET"],
                "url_prefix": (
                    "https://example.com:443/search?"
                    "q=factory+yield+99.9%25+%23release"
                ),
                "query_exact": True,
            }],
            [],
        )
        server._authorize_exact_request(
            policy,
            self.origin,
            self._request(
                "GET",
                "/search?q=factory+yield+99.9%25+%23release",
            ),
        )

    def test_policy_rejects_origin_projection_and_private_mismatch(self):
        cases = (
            (
                ["https://other.example:443"],
                [{
                    "methods": ["GET"],
                    "url_prefix": "https://example.com:443/",
                }],
                [],
            ),
            (
                ["https://example.com:443"],
                [{
                    "methods": ["GET"],
                    "url_prefix": "https://example.com:443/",
                }],
                ["https://other.example:443"],
            ),
            (
                ["https://example.com:443"],
                [{
                    "methods": ["HEAD", "GET"],
                    "url_prefix": "https://example.com:443/",
                }],
                [],
            ),
        )
        for origins, rules, private in cases:
            with self.subTest(
                origins=origins,
                rules=rules,
                private=private,
            ), self.assertRaises(server.ProxyPolicyError):
                server._validated_signed_egress_policy(
                    origins,
                    rules,
                    private,
                )

    def test_public_read_is_generic_read_only_and_sanitized(self):
        policy = server._validated_signed_egress_policy(
            [],
            [],
            [],
            version=3,
            budget_scope_sha256="a" * 64,
            call_id_sha256="b" * 64,
            limits_raw={
                "max_requests": 8,
                "max_outbound_bytes": 4096,
                "max_response_wire_bytes": 65536,
            },
            public_read_raw={
                "methods": ["GET", "HEAD"],
                "ports": [80, 443],
            },
        )
        for hostname in ("manuals.example", "packages.example"):
            origin = server.Origin("https", hostname, 443)
            request = (
                f"GET /resource?q=public HTTP/1.1\r\n"
                f"Host: {hostname}\r\n"
                "Authorization: Bearer must-not-leak\r\n"
                "Cookie: secret=value\r\n"
                "X-Workspace-Token: must-not-leak\r\n\r\n"
            ).encode("ascii")
            self.assertEqual(
                "public_read",
                server._authorize_request(policy, origin, request),
            )
            sanitized = server._sanitize_public_read_request(
                request,
                origin,
            )
            self.assertIn(b"ChatDS-PublicRead/1.0", sanitized)
            self.assertNotIn(b"Authorization", sanitized)
            self.assertNotIn(b"Cookie", sanitized)
            self.assertNotIn(b"Workspace-Token", sanitized)

        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "request_url_not_allowed",
        ):
            server._authorize_request(
                policy,
                server.Origin("https", "manuals.example", 443),
                self._request("POST", "/resource"),
            )

    def test_public_read_profile_is_exact_and_hmac_bound(self):
        profile = {"methods": ["GET", "HEAD"], "ports": [80, 443]}
        rendered = _policy_preface(
            [],
            egress_rules=[],
            private_origins=[],
            version=3,
            public_read=profile,
        )
        payload = json.loads(
            rendered[len(server.POLICY_PREFACE_PREFIX):].decode("utf-8")
        )
        payload["public_read"] = None
        with (
            patch.dict(
                os.environ,
                {"SKILL_EGRESS_POLICY_TOKEN": TEST_POLICY_TOKEN},
            ),
            self.assertRaisesRegex(
                server.ProxyPolicyError,
                "policy_authentication_failed",
            ),
        ):
            PolicyPrefaceAuthenticationTests._read_preface(
                server.POLICY_PREFACE_PREFIX
                + json.dumps(payload, separators=(",", ":")).encode()
                + b"\n"
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
            result = policy.resolve(
                "https",
                "EXAMPLE.com.",
                443,
                origin_allowlist=(("HTTPS", "example.COM.", 443),),
            )
        self.assertEqual("example.com", result.host)
        self.assertEqual("93.184.216.34", result.address)
        self.assertFalse(result.private_grant)

    def test_public_read_allows_only_global_standard_port_answers(self):
        policy = server.AddressPolicy(public_ports=(80, 443))
        with patch.object(
            server.socket,
            "getaddrinfo",
            return_value=self._records("93.184.216.34"),
        ):
            result = policy.resolve(
                "https",
                "renamed-holdout.example",
                443,
                public_read=True,
            )
        self.assertEqual("93.184.216.34", result.address)

        for records in (
            self._records("127.0.0.1"),
            self._records("93.184.216.34", "10.0.0.7"),
        ):
            with (
                patch.object(
                    server.socket,
                    "getaddrinfo",
                    return_value=records,
                ),
                self.assertRaisesRegex(
                    server.ProxyPolicyError,
                    "destination_address_not_public",
                ),
            ):
                policy.resolve(
                    "https",
                    "renamed-holdout.example",
                    443,
                    public_read=True,
                )
        with (
            patch.object(server.socket, "getaddrinfo") as resolver,
            self.assertRaisesRegex(
                server.ProxyPolicyError,
                "destination_port_not_allowed",
            ),
        ):
            policy.resolve(
                "https",
                "renamed-holdout.example",
                8443,
                public_read=True,
            )
        resolver.assert_not_called()

    def test_default_deny_and_origin_mismatch_happen_before_dns(self):
        policy = server.AddressPolicy(public_ports=(80, 443))
        attempts = (
            (
                ("https", "example.com", 443),
                None,
            ),
            (
                ("https", "sub.example.com", 443),
                (("https", "example.com", 443),),
            ),
            (
                ("http", "example.com", 80),
                (("https", "example.com", 443),),
            ),
            (
                ("https", "example.com", 8443),
                (("https", "example.com", 443),),
            ),
        )
        with patch.object(server.socket, "getaddrinfo") as resolver:
            for destination, allowlist in attempts:
                with self.subTest(destination=destination), self.assertRaisesRegex(
                    server.ProxyPolicyError,
                    "destination_origin_not_allowed",
                ):
                    policy.resolve(
                        *destination,
                        origin_allowlist=allowlist,
                    )
        resolver.assert_not_called()

    def test_authorized_dns_failures_are_transport_not_policy(self):
        policy = server.AddressPolicy(public_ports=(80, 443))
        origin = (("https", "example.com", 443),)
        cases = (
            (
                socket.gaierror("resolver-sensitive-detail"),
                server.ProxyTransportError,
                "destination_dns_failed",
                502,
            ),
            (
                socket.timeout("resolver-sensitive-detail"),
                server.ProxyTransportTimeoutError,
                "destination_dns_timeout",
                504,
            ),
        )
        for failure, error_type, reason, status in cases:
            with (
                self.subTest(reason=reason),
                patch.object(
                    server.socket,
                    "getaddrinfo",
                    side_effect=failure,
                ),
                self.assertRaisesRegex(error_type, reason) as raised,
            ):
                policy.resolve(
                    "https",
                    "example.com",
                    443,
                    origin_allowlist=origin,
                )
            self.assertEqual(
                status,
                server._transport_error_status(raised.exception),
            )
            self.assertNotIn(
                "resolver-sensitive-detail",
                str(raised.exception),
            )

        with (
            patch.object(
                server.socket,
                "getaddrinfo",
                return_value=[],
            ),
            self.assertRaisesRegex(
                server.ProxyTransportError,
                "destination_dns_empty",
            ) as raised,
        ):
            policy.resolve(
                "https",
                "example.com",
                443,
                origin_allowlist=origin,
            )
        self.assertEqual(
            502,
            server._transport_error_status(raised.exception),
        )

    def test_allowlist_is_exact_normalized_and_forbids_wildcards(self):
        normalized = server.normalize_origin_allowlist((
            ("HTTPS", "EXAMPLE.com.", 443),
            server.Origin("https", "example.com", 443),
            ("http", "example.com", 80),
        ))
        self.assertEqual(
            frozenset({
                server.Origin("https", "example.com", 443),
                server.Origin("http", "example.com", 80),
            }),
            normalized,
        )
        for host in (
            "*.example.com",
            "api.*.example.com",
            "example.?",
            "[example.com]",
        ):
            with self.subTest(host=host), self.assertRaisesRegex(
                server.ProxyPolicyError,
                "invalid_destination_host",
            ):
                server.normalize_origin_allowlist((
                    ("https", host, 443),
                ))

    def test_allowlist_rejects_malformed_and_unbounded_entries(self):
        for allowlist in (
            "https://example.com",
            (("https", "example.com", "443"),),
            (("https", "example.com", True),),
            (("https", "example.com", 0),),
            (("https", "example.com"),),
        ):
            with self.subTest(allowlist=allowlist), self.assertRaises(
                server.ProxyPolicyError,
            ):
                server.normalize_origin_allowlist(allowlist)
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "origin_allowlist_too_large",
        ):
            server.normalize_origin_allowlist(
                (
                    ("https", f"host-{index}.example.com", 443)
                    for index in range(
                        server.MAX_ORIGIN_ALLOWLIST_ENTRIES + 1
                    )
                )
            )

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
                    policy.resolve(
                        "https",
                        "example.test",
                        443,
                        origin_allowlist=(
                            ("https", "example.test", 443),
                        ),
                    )

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
                origin_allowlist=(
                    ("https", "internal.example", 8443),
                ),
                signed_private_origins=(
                    ("https", "internal.example", 8443),
                ),
            )
            self.assertTrue(granted.private_grant)
            with self.assertRaisesRegex(
                server.ProxyPolicyError,
                "destination_port_not_allowed",
            ):
                policy.resolve(
                    "http",
                    "internal.example",
                    8443,
                    origin_allowlist=(
                        ("http", "internal.example", 8443),
                    ),
                )

    def test_private_origin_requires_deployment_and_signed_grants(self):
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
            for signed_private in (
                (),
                (("https", "other.example", 8443),),
            ):
                with (
                    self.subTest(signed_private=signed_private),
                    self.assertRaises(server.ProxyPolicyError),
                ):
                    policy.resolve(
                        "https",
                        "internal.example",
                        8443,
                        origin_allowlist=(
                            ("https", "internal.example", 8443),
                        ),
                        signed_private_origins=signed_private,
                    )

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
                    policy.resolve(
                        "https",
                        "internal.example",
                        8443,
                        origin_allowlist=(
                            ("https", "internal.example", 8443),
                        ),
                        signed_private_origins=(
                            ("https", "internal.example", 8443),
                        ),
                    )

    def test_exact_private_ip_origin_is_self_pinning_without_redundant_cidr(self):
        policy = server.AddressPolicy(
            public_ports=(80, 443),
            private_origins=("https://10.10.132.126:18443",),
            private_cidrs=(),
        )
        with patch.object(
            server.socket,
            "getaddrinfo",
            return_value=self._records("10.10.132.126"),
        ):
            granted = policy.resolve(
                "https",
                "10.10.132.126",
                18443,
                origin_allowlist=(
                    ("https", "10.10.132.126", 18443),
                ),
                signed_private_origins=(
                    ("https", "10.10.132.126", 18443),
                ),
            )
        self.assertTrue(granted.private_grant)
        self.assertEqual("10.10.132.126", granted.address)

        with patch.object(
            server.socket,
            "getaddrinfo",
            return_value=self._records("10.10.132.127"),
        ), self.assertRaisesRegex(
            server.ProxyPolicyError,
            "destination_address_outside_private_literal_origin",
        ):
            policy.resolve(
                "https",
                "10.10.132.126",
                18443,
                origin_allowlist=(
                    ("https", "10.10.132.126", 18443),
                ),
                signed_private_origins=(
                    ("https", "10.10.132.126", 18443),
                ),
            )

    def test_public_non_web_port_is_blocked_before_dns(self):
        policy = server.AddressPolicy(public_ports=(80, 443))
        with patch.object(server.socket, "getaddrinfo") as resolver:
            with self.assertRaisesRegex(
                server.ProxyPolicyError,
                "destination_port_not_allowed",
            ):
                policy.resolve(
                    "https",
                    "example.com",
                    22,
                    origin_allowlist=(
                        ("https", "example.com", 22),
                    ),
                )
        resolver.assert_not_called()

    def test_absolute_http_default_port_matches_only_http_origin(self):
        scheme, host, port, _forwarded = server._request_destination(
            b"GET http://EXAMPLE.com:080/proof HTTP/1.1\r\n"
            b"Host: example.com\r\n\r\n"
        )
        self.assertEqual(("http", "example.com", 80), (scheme, host, port))
        policy = server.AddressPolicy(public_ports=(80, 443))
        with patch.object(
            server.socket,
            "getaddrinfo",
            return_value=self._records("93.184.216.34"),
        ):
            destination = policy.resolve(
                scheme,
                host,
                port,
                origin_allowlist=(("HTTP", "example.com.", 80),),
            )
        self.assertEqual(
            ("http", "example.com", 80),
            (destination.scheme, destination.host, destination.port),
        )


class TlsInterceptionPolicyTests(unittest.TestCase):
    def test_tunneled_https_requires_one_origin_form_http11_request(self):
        expected = server.Origin("https", "example.com", 443)
        forwarded = server._tunneled_https_request(
            b"GET /proof?q=1 HTTP/1.1\r\n"
            b"Host: EXAMPLE.com.:443\r\n"
            b"Connection: keep-alive\r\n\r\n",
            expected,
        )
        self.assertTrue(forwarded.startswith(
            b"GET /proof?q=1 HTTP/1.1\r\n"
        ))
        self.assertEqual(
            1,
            forwarded.count(b"Host: example.com\r\n"),
        )
        self.assertEqual(
            1,
            forwarded.count(b"Connection: close\r\n"),
        )

        rejected = (
            (
                b"CONNECT other.example:443 HTTP/1.1\r\n"
                b"Host: example.com\r\n\r\n",
                "nested_connect_not_allowed",
            ),
            (
                b"GET https://example.com/ HTTP/1.1\r\n"
                b"Host: example.com\r\n\r\n",
                "origin_form_https_target_required",
            ),
            (
                b"PRI * HTTP/2.0\r\n\r\n",
                "unsupported_http_version",
            ),
            (
                b"GET / HTTP/1.1\r\n"
                b"Host: other.example\r\n\r\n",
                "https_host_origin_mismatch",
            ),
            (
                b"GET / HTTP/1.1\r\n"
                b"Host: example.com\r\n"
                b"Upgrade: websocket\r\n\r\n",
                "http_upgrade_not_supported",
            ),
        )
        for request, reason in rejected:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                server.ProxyPolicyError,
                reason,
            ):
                server._tunneled_https_request(request, expected)

    def test_private_tls_requires_exact_or_legacy_deployment_pin(self):
        pin = "A" * 43 + "="
        private = server.Destination(
            "https",
            "10.0.0.8",
            8443,
            "10.0.0.8",
            socket.AF_INET,
            True,
        )
        public = server.Destination(
            "https",
            "example.com",
            443,
            "93.184.216.34",
            socket.AF_INET,
            False,
        )
        with self.assertRaisesRegex(
            server.ProxyPolicyError,
            "upstream_tls_spki_pin_required",
        ):
            server.UpstreamTlsPolicy(
                {},
                legacy_private_pins=frozenset(),
            ).authorize(private)

        legacy = server.UpstreamTlsPolicy(
            {},
            legacy_private_pins=frozenset({pin}),
        )
        legacy.authorize(private)
        legacy.authorize(public)
        self.assertNotIn(
            server.Origin("https", "example.com", 443),
            legacy.pins,
        )

    def test_public_tls_failures_keep_distinct_safe_codes(self):
        destination = server.Destination(
            "https", "example.com", 443, "93.184.216.34",
            socket.AF_INET, False,
        )
        policy = server.UpstreamTlsPolicy({})

        class RawSocket:
            def settimeout(self, _value):
                return None

        class Context:
            post_handshake_auth = False
            check_hostname = True
            verify_mode = ssl.CERT_REQUIRED
            minimum_version = ssl.TLSVersion.TLSv1_2

            def __init__(self, failure):
                self.failure = failure

            def set_alpn_protocols(self, _protocols):
                return None

            def wrap_socket(self, *_args, **_kwargs):
                raise self.failure

        cases = (
            (
                ssl.SSLCertVerificationError(1, "fixture"),
                "upstream_tls_certificate_invalid",
            ),
            (socket.timeout("fixture"), "upstream_tls_handshake_timeout"),
            (ConnectionResetError("fixture"), "upstream_connection_reset"),
            (ssl.SSLError("fixture"), "upstream_tls_handshake_failed"),
            (OSError("fixture"), "upstream_tls_transport_failed"),
        )
        for failure, code in cases:
            with (
                self.subTest(code=code),
                patch.object(
                    server.ssl,
                    "create_default_context",
                    return_value=Context(failure),
                ),
                self.assertRaisesRegex(server.ProxyPolicyError, code),
            ):
                policy.wrap(RawSocket(), destination)

    def test_legacy_pin_parser_is_bounded_and_canonicalizes_padding(self):
        pin_without_padding = "A" * 43
        self.assertEqual(
            frozenset({pin_without_padding + "="}),
            server._parse_legacy_private_tls_pins(
                pin_without_padding
            ),
        )
        for value in (
            "not-a-pin",
            ",".join(["A" * 43] * 9),
            "A" * 43 + "," + "A" * 43,
        ):
            with self.subTest(value=value), self.assertRaises(
                server.ProxyPolicyError,
            ):
                server._parse_legacy_private_tls_pins(value)

    def test_exact_origin_pin_configuration_is_not_a_wildcard(self):
        pin = "A" * 43 + "="
        parsed = server._parse_upstream_tls_pins(json.dumps({
            "https://internal.example:8443": [pin],
        }))
        self.assertEqual(
            frozenset({pin}),
            parsed[
                server.Origin(
                    "https",
                    "internal.example",
                    8443,
                )
            ],
        )
        for origin in (
            "https://*.example.com:443",
            "http://internal.example:80",
            "https://internal.example:0",
        ):
            with self.subTest(origin=origin), self.assertRaises(
                server.ProxyPolicyError,
            ):
                server._parse_upstream_tls_pins(json.dumps({
                    origin: [pin],
                }))

    def test_ca_restart_reuses_generation_and_keeps_keys_private(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = root / "public"
            private = root / "private"
            public.mkdir()
            private.mkdir()
            public.chmod(0o2710)
            private.chmod(0o700)
            first = server.CertificateAuthority(
                public_directory=public,
                private_directory=private,
            )
            first_ca = (public / "ca.pem").read_bytes()
            first_spki = (public / "leaf.spki").read_bytes()
            first_manifest = (public / "generation.json").read_bytes()
            first_generation = first.generation_id
            first_runtime = first.runtime_directory
            self.assertTrue((private / "ca.key").is_file())
            self.assertTrue((private / "leaf.key").is_file())
            initial_context = first.server_context("example.com")
            issued_at = first._contexts["example.com"][1]
            with patch.object(
                server.time,
                "monotonic",
                return_value=(
                    issued_at
                    + server.INTERCEPTION_CERTIFICATE_REFRESH_SECONDS
                    + 1
                ),
            ):
                refreshed_context = first.server_context(
                    "example.com"
                )
            self.assertIsNot(initial_context, refreshed_context)
            self.assertEqual(
                first_spki,
                (public / "leaf.spki").read_bytes(),
            )
            self.assertEqual(
                {"ca.pem", "leaf.spki", "generation.json"},
                {path.name for path in public.iterdir()},
            )
            self.assertFalse(any(
                path.suffix == ".key"
                for path in public.iterdir()
            ))
            self.assertEqual(
                0o440,
                stat.S_IMODE((public / "ca.pem").stat().st_mode),
            )
            first.close()
            self.assertFalse(first_runtime.exists())

            second = server.CertificateAuthority(
                public_directory=public,
                private_directory=private,
            )
            try:
                self.assertEqual(
                    first_ca,
                    (public / "ca.pem").read_bytes(),
                )
                self.assertEqual(
                    first_spki,
                    (public / "leaf.spki").read_bytes(),
                )
                self.assertEqual(
                    first_manifest,
                    (public / "generation.json").read_bytes(),
                )
                self.assertEqual(
                    first_generation,
                    second.generation_id,
                )
                self.assertEqual(
                    {"ca.pem", "leaf.spki", "generation.json"},
                    {path.name for path in public.iterdir()},
                )
            finally:
                second.close()

    def test_public_publish_persists_final_mode_before_directory_commit(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary) / "public"
            public.mkdir()
            public.chmod(0o2710)
            destination = public / "ca.pem"
            events: list[tuple[str, str | int]] = []
            original_fsync = server.os.fsync
            original_fchmod = server.os.fchmod
            original_replace = server.os.replace

            def recording_fsync(descriptor: int) -> None:
                metadata = os.fstat(descriptor)
                events.append((
                    "fsync",
                    (
                        "directory"
                        if stat.S_ISDIR(metadata.st_mode)
                        else stat.S_IMODE(metadata.st_mode)
                    ),
                ))
                original_fsync(descriptor)

            def recording_fchmod(
                descriptor: int,
                mode: int,
            ) -> None:
                events.append(("fchmod", mode))
                original_fchmod(descriptor, mode)

            def recording_replace(
                source: Path,
                target: Path,
            ) -> None:
                events.append(("replace", target.name))
                original_replace(source, target)

            with (
                patch.object(
                    server.os,
                    "fsync",
                    side_effect=recording_fsync,
                ),
                patch.object(
                    server.os,
                    "fchmod",
                    side_effect=recording_fchmod,
                ),
                patch.object(
                    server.os,
                    "replace",
                    side_effect=recording_replace,
                ),
            ):
                server._atomic_publish_public_file(
                    destination,
                    b"public-ca-content",
                    mode=0o440,
                )

            self.assertEqual(
                [
                    ("fsync", 0o400),
                    ("fchmod", 0o440),
                    ("fsync", 0o440),
                    ("replace", "ca.pem"),
                    ("fsync", "directory"),
                ],
                events,
            )
            self.assertEqual(
                0o440,
                stat.S_IMODE(destination.stat().st_mode),
            )

    def test_ca_recovers_owned_private_first_generation_prefixes(
        self,
    ):
        for retained_names in (
            {"ca.key"},
            {"ca.key", "ca.pem"},
        ):
            with self.subTest(retained=sorted(retained_names)):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    public = root / "public"
                    private = root / "private"
                    public.mkdir()
                    private.mkdir()
                    public.chmod(0o2710)
                    private.chmod(0o700)
                    authority = server.CertificateAuthority(
                        public_directory=public,
                        private_directory=private,
                    )
                    authority.close()
                    for path in public.iterdir():
                        path.unlink()
                    for path in private.iterdir():
                        if path.name not in retained_names:
                            path.unlink()

                    recovered = server.CertificateAuthority(
                        public_directory=public,
                        private_directory=private,
                    )
                    try:
                        self.assertEqual(
                            {"ca.key", "ca.pem", "leaf.key"},
                            {path.name for path in private.iterdir()},
                        )
                        self.assertEqual(
                            {"ca.pem", "leaf.spki", "generation.json"},
                            {path.name for path in public.iterdir()},
                        )
                    finally:
                        recovered.close()

    def test_ca_recovers_complete_but_interrupted_unpublished_generation(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = root / "public"
            private = root / "private"
            public.mkdir()
            private.mkdir()
            public.chmod(0o2710)
            private.chmod(0o700)
            authority = server.CertificateAuthority(
                public_directory=public,
                private_directory=private,
            )
            old_generation = authority.generation_id
            authority.close()
            for path in public.iterdir():
                path.unlink()
            interrupted = private / "leaf.key"
            interrupted.chmod(0o600)
            interrupted.write_bytes(b"truncated-key")
            interrupted.chmod(0o400)

            recovered = server.CertificateAuthority(
                public_directory=public,
                private_directory=private,
            )
            try:
                self.assertNotEqual(
                    old_generation,
                    recovered.generation_id,
                )
                self.assertEqual(
                    {"ca.key", "ca.pem", "leaf.key"},
                    {path.name for path in private.iterdir()},
                )
                self.assertEqual(
                    {"ca.pem", "leaf.spki", "generation.json"},
                    {path.name for path in public.iterdir()},
                )
            finally:
                recovered.close()

    def test_ca_rejects_unsafe_or_published_incomplete_private_state(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = root / "public"
            private = root / "private"
            public.mkdir()
            private.mkdir()
            public.chmod(0o2710)
            private.chmod(0o700)
            (private / "ca.key").symlink_to(root / "outside")
            with self.assertRaisesRegex(
                server.ProxyPolicyError,
                "unsafe_incomplete_private_trust_material",
            ):
                server.CertificateAuthority(
                    public_directory=public,
                    private_directory=private,
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = root / "public"
            private = root / "private"
            public.mkdir()
            private.mkdir()
            public.chmod(0o2710)
            private.chmod(0o700)
            authority = server.CertificateAuthority(
                public_directory=public,
                private_directory=private,
            )
            authority.close()
            (private / "leaf.key").unlink()
            with self.assertRaisesRegex(
                server.ProxyPolicyError,
                "incomplete_private_trust_material",
            ):
                server.CertificateAuthority(
                    public_directory=public,
                    private_directory=private,
                )

    def test_ca_recovers_only_matching_ordered_public_prefixes(self):
        for retained_names in (
            {"ca.pem"},
            {"ca.pem", "leaf.spki"},
        ):
            with self.subTest(retained=sorted(retained_names)):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    public = root / "public"
                    private = root / "private"
                    public.mkdir()
                    private.mkdir()
                    public.chmod(0o2710)
                    private.chmod(0o700)
                    authority = server.CertificateAuthority(
                        public_directory=public,
                        private_directory=private,
                    )
                    expected = {
                        path.name: path.read_bytes()
                        for path in public.iterdir()
                    }
                    authority.close()
                    for path in public.iterdir():
                        if path.name not in retained_names:
                            path.unlink()

                    recovered = server.CertificateAuthority(
                        public_directory=public,
                        private_directory=private,
                    )
                    try:
                        self.assertEqual(
                            expected,
                            {
                                path.name: path.read_bytes()
                                for path in public.iterdir()
                            },
                        )
                    finally:
                        recovered.close()

    def test_ca_public_publish_is_restart_safe_after_each_commit_step(
        self,
    ):
        original_publish = server._atomic_publish_public_file
        for crash_after in (1, 2, 3):
            with self.subTest(crash_after=crash_after):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    public = root / "public"
                    private = root / "private"
                    public.mkdir()
                    private.mkdir()
                    public.chmod(0o2710)
                    private.chmod(0o700)
                    published = 0

                    def publish_then_crash(
                        path: Path,
                        content: bytes,
                        *,
                        mode: int,
                    ) -> None:
                        nonlocal published
                        original_publish(path, content, mode=mode)
                        published += 1
                        if published == crash_after:
                            raise RuntimeError("injected host crash")

                    with patch.object(
                        server,
                        "_atomic_publish_public_file",
                        side_effect=publish_then_crash,
                    ), self.assertRaisesRegex(
                        RuntimeError,
                        "injected host crash",
                    ):
                        server.CertificateAuthority(
                            public_directory=public,
                            private_directory=private,
                        )

                    recovered = server.CertificateAuthority(
                        public_directory=public,
                        private_directory=private,
                    )
                    try:
                        self.assertEqual(
                            {"ca.pem", "leaf.spki", "generation.json"},
                            {path.name for path in public.iterdir()},
                        )
                        self.assertEqual(
                            {"ca.key", "ca.pem", "leaf.key"},
                            {path.name for path in private.iterdir()},
                        )
                    finally:
                        recovered.close()

    def test_mixed_public_generation_and_private_key_mismatch_fail_closed(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = root / "public"
            private = root / "private"
            public.mkdir()
            private.mkdir()
            public.chmod(0o2710)
            private.chmod(0o700)
            authority = server.CertificateAuthority(
                public_directory=public,
                private_directory=private,
            )
            authority.close()

            spki_path = public / "leaf.spki"
            spki_path.chmod(0o600)
            spki_path.write_text("A" * 43 + "=\n", encoding="ascii")
            spki_path.chmod(0o440)
            with self.assertRaisesRegex(
                server.ProxyPolicyError,
                "mixed_public_trust_generation",
            ):
                server.CertificateAuthority(
                    public_directory=public,
                    private_directory=private,
                )

            for path in public.iterdir():
                path.unlink()
            public_ca = authority.public_ca_path
            public_ca.write_bytes(b"tampered\n")
            public_ca.chmod(0o440)
            with self.assertRaisesRegex(
                server.ProxyPolicyError,
                "mixed_public_trust_generation",
            ):
                server.CertificateAuthority(
                    public_directory=public,
                    private_directory=private,
                )

            for path in public.iterdir():
                path.unlink()
            republished = server.CertificateAuthority(
                public_directory=public,
                private_directory=private,
            )
            republished.close()
            ca_key = private / "ca.key"
            ca_key.unlink()
            server._run_openssl([
                "genpkey",
                "-algorithm",
                "EC",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-out",
                str(ca_key),
            ])
            ca_key.chmod(0o400)
            with self.assertRaisesRegex(
                server.ProxyPolicyError,
                "private_ca_key_mismatch",
            ):
                server.CertificateAuthority(
                    public_directory=public,
                    private_directory=private,
                )


class ProxyRelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"SKILL_EGRESS_POLICY_TOKEN": TEST_POLICY_TOKEN},
        )
        self.environment.start()
        self.ca_temporary = tempfile.TemporaryDirectory()
        trust_directory = Path(self.ca_temporary.name) / "trust"
        trust_directory.mkdir(mode=0o700)
        trust_directory.chmod(0o2710)
        private_directory = Path(self.ca_temporary.name) / "private"
        private_directory.mkdir(mode=0o700)
        self.certificate_authority = server.CertificateAuthority(
            public_directory=trust_directory,
            private_directory=private_directory,
        )
        server.ProxyHandler.certificate_authority = (
            self.certificate_authority
        )
        server.ProxyHandler.upstream_tls_policy = (
            server.UpstreamTlsPolicy({})
        )
        server.ProxyHandler.trust_generation = (
            self.certificate_authority.generation_id
        )
        self.previous_scope_ledger = server.ProxyHandler.scope_ledger
        server.ProxyHandler.scope_ledger = server.PolicyScopeLedger()

    def tearDown(self):
        server.ProxyHandler.certificate_authority = None
        server.ProxyHandler.trust_generation = None
        server.ProxyHandler.upstream_tls_policy = (
            server.UpstreamTlsPolicy({})
        )
        server.ProxyHandler.scope_ledger = self.previous_scope_ledger
        self.certificate_authority.close()
        self.ca_temporary.cleanup()
        self.environment.stop()

    def test_upstream_write_timeout_is_gateway_timeout_in_all_http_lanes(
        self,
    ):
        class RecordingSocket:
            def __init__(self) -> None:
                self.payload = bytearray()
                self.closed = False

            def sendall(self, content: bytes) -> None:
                self.payload.extend(content)

            def recv(self, _size: int) -> bytes:
                return b""

            def settimeout(self, _value: float) -> None:
                return

            def shutdown(self, _how: int) -> None:
                return

            def close(self) -> None:
                self.closed = True

            def selected_alpn_protocol(self):
                return None

        class FixedPolicy:
            def __init__(self, destination):
                self.destination = destination

            def resolve(self, *_args, **_kwargs):
                return self.destination

        class TlsContext:
            def __init__(self, client_tls):
                self.client_tls = client_tls

            def wrap_socket(self, *_args, **_kwargs):
                return self.client_tls

        class Authority:
            generation_id = TEST_TRUST_GENERATION

            def __init__(self, client_tls):
                self.client_tls = client_tls

            def server_context(self, _host: str):
                return TlsContext(self.client_tls)

        class TlsPolicy:
            def authorize(self, _destination) -> None:
                return

            def wrap(self, raw_socket, _destination):
                return raw_socket

        origin = server.Origin("http", "example.com", 80)
        signed_http = server.SignedEgressPolicy(
            origins=frozenset({origin}),
            rules=(
                server.ExactEgressRule(
                    methods=frozenset({"POST"}),
                    origin=origin,
                    path_prefix="/upload",
                    query_prefix="",
                ),
            ),
            private_origins=frozenset(),
        )
        absolute_request = (
            b"POST http://example.com/upload HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        tunneled_request = (
            b"POST /upload HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        destination_http = server.Destination(
            "http",
            "example.com",
            80,
            "203.0.113.10",
            socket.AF_INET,
            False,
        )

        for lane, first_request in (
            ("absolute-http", absolute_request),
            (
                "connect-http",
                b"CONNECT example.com:80 HTTP/1.1\r\n"
                b"Host: example.com:80\r\n\r\n",
            ),
        ):
            with self.subTest(lane=lane):
                client = RecordingSocket()
                upstream = RecordingSocket()
                handler = object.__new__(server.ProxyHandler)
                handler.request = client
                handler.policy = FixedPolicy(destination_http)
                handler.certificate_authority = None
                handler.upstream_tls_policy = TlsPolicy()
                handler.trust_generation = TEST_TRUST_GENERATION
                requests = (
                    [first_request]
                    if lane == "absolute-http"
                    else [first_request, tunneled_request]
                )
                with (
                    patch.object(
                        server,
                        "_read_policy_preface",
                        return_value=(signed_http, b""),
                    ),
                    patch.object(
                        server,
                        "_read_headers",
                        side_effect=requests,
                    ),
                    patch.object(
                        server,
                        "_connect_pinned",
                        return_value=upstream,
                    ),
                    patch.object(
                        server,
                        "_forward_single_http_request",
                        side_effect=server.ProxyTransportTimeoutError(
                            "upstream_request_write_timeout"
                        ),
                    ),
                ):
                    handler.handle()
                self.assertIn(
                    b"HTTP/1.1 504 upstream_request_write_timeout",
                    client.payload,
                )
                self.assertNotIn(b"HTTP/1.1 403", client.payload)

        https_origin = server.Origin("https", "example.com", 443)
        signed_https = server.SignedEgressPolicy(
            origins=frozenset({https_origin}),
            rules=(
                server.ExactEgressRule(
                    methods=frozenset({"POST"}),
                    origin=https_origin,
                    path_prefix="/upload",
                    query_prefix="",
                ),
            ),
            private_origins=frozenset(),
        )
        destination_https = server.Destination(
            "https",
            "example.com",
            443,
            "203.0.113.10",
            socket.AF_INET,
            False,
        )
        client = RecordingSocket()
        client_tls = RecordingSocket()
        upstream = RecordingSocket()
        handler = object.__new__(server.ProxyHandler)
        handler.request = client
        handler.policy = FixedPolicy(destination_https)
        handler.certificate_authority = Authority(client_tls)
        handler.upstream_tls_policy = TlsPolicy()
        handler.trust_generation = TEST_TRUST_GENERATION
        with (
            patch.object(
                server,
                "_read_policy_preface",
                return_value=(signed_https, b""),
            ),
            patch.object(
                server,
                "_read_headers",
                side_effect=[
                    (
                        b"CONNECT example.com:443 HTTP/1.1\r\n"
                        b"Host: example.com:443\r\n\r\n"
                    ),
                    (
                        b"POST /upload HTTP/1.1\r\n"
                        b"Host: example.com\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    ),
                ],
            ),
            patch.object(
                server,
                "_connect_pinned",
                return_value=upstream,
            ),
            patch.object(
                server,
                "_forward_single_http_request",
                side_effect=server.ProxyTransportTimeoutError(
                    "upstream_request_write_timeout"
                ),
            ),
        ):
            handler.handle()
        self.assertIn(
            b"HTTP/1.1 504 upstream_request_write_timeout",
            client_tls.payload,
        )
        self.assertNotIn(b"HTTP/1.1 403", client_tls.payload)

    def test_dns_failure_is_bad_gateway_in_all_http_lanes(self):
        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            server.ProxyHandler,
        )
        proxy_thread = threading.Thread(
            target=proxy.serve_forever,
            daemon=True,
        )
        proxy_thread.start()
        requests = (
            (
                "absolute-http",
                "http://example.com:80",
                (
                    b"GET http://example.com/proof HTTP/1.1\r\n"
                    b"Host: example.com\r\n\r\n"
                ),
            ),
            (
                "connect-http",
                "http://example.com:80",
                (
                    b"CONNECT example.com:80 HTTP/1.1\r\n"
                    b"Host: example.com:80\r\n\r\n"
                ),
            ),
            (
                "connect-https-mitm",
                "https://example.com:443",
                (
                    b"CONNECT example.com:443 HTTP/1.1\r\n"
                    b"Host: example.com:443\r\n\r\n"
                ),
            ),
        )
        try:
            for lane, origin, request in requests:
                with self.subTest(lane=lane):
                    client = socket.socket(
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                    )
                    try:
                        client.settimeout(3)
                        client.connect(proxy.server_address)
                        with patch.object(
                            server.socket,
                            "getaddrinfo",
                            side_effect=socket.gaierror(
                                "resolver-sensitive-detail"
                            ),
                        ):
                            client.sendall(
                                _policy_preface([origin]) + request
                            )
                            response = bytearray()
                            while True:
                                chunk = client.recv(4096)
                                if not chunk:
                                    break
                                response.extend(chunk)
                    finally:
                        client.close()
                    self.assertTrue(
                        response.startswith(
                            b"HTTP/1.1 502 destination_dns_failed"
                        ),
                        response,
                    )
                    self.assertNotIn(
                        b"resolver-sensitive-detail",
                        response,
                    )
                    self.assertNotIn(b"HTTP/1.1 403", response)
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=3)

    def test_typed_pre_tunnel_error_half_closes_then_drains_input(self):
        proxy_side, bridge_side = socket.socketpair()
        completed = threading.Event()

        def reject() -> None:
            try:
                server._safe_error_then_drain(
                    proxy_side,
                    403,
                    "policy_trust_generation_mismatch",
                )
            finally:
                completed.set()

        worker = threading.Thread(target=reject, daemon=True)
        try:
            # Model the browser request which can arrive immediately after the
            # signed preface and remain unread when the proxy rejects it.
            bridge_side.sendall(
                b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
            )
            worker.start()
            response = bytearray()
            while True:
                chunk = bridge_side.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
            bridge_side.shutdown(socket.SHUT_WR)
            worker.join(timeout=2)
            self.assertTrue(completed.is_set())
            self.assertTrue(
                response.startswith(b"HTTP/1.1 403 "),
                response,
            )
            self.assertIn(
                b"policy_trust_generation_mismatch",
                response,
            )
        finally:
            proxy_side.close()
            bridge_side.close()
            worker.join(timeout=2)

    def test_raw_unsigned_request_is_default_denied_before_dns(self):
        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            server.ProxyHandler,
        )
        proxy_thread = threading.Thread(
            target=proxy.serve_forever,
            daemon=True,
        )
        proxy_thread.start()
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.settimeout(3)
                client.connect(
                    ("127.0.0.1", int(proxy.server_address[1]))
                )
                with patch.object(server.socket, "getaddrinfo") as resolver:
                    client.sendall(
                        b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
                    )
                    response = bytearray()
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        response.extend(chunk)
                resolver.assert_not_called()
            finally:
                client.close()
            self.assertTrue(response.startswith(b"HTTP/1.1 403 "))
            self.assertIn(
                b"authenticated_policy_preface_required",
                response,
            )
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=3)

    def test_v3_outbound_budget_is_rejected_before_dns(self):
        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            server.ProxyHandler,
        )
        proxy_thread = threading.Thread(
            target=proxy.serve_forever,
            daemon=True,
        )
        proxy_thread.start()
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.settimeout(3)
                client.connect(proxy.server_address)
                with patch.object(server.socket, "getaddrinfo") as resolver:
                    client.sendall(
                        _policy_preface(
                            ["http://allowed.example:80"],
                            version=3,
                            limits={
                                "max_requests": 1,
                                "max_outbound_bytes": 1,
                                "max_response_wire_bytes": 1,
                            },
                        )
                        + (
                            b"GET http://allowed.example/proof HTTP/1.1\r\n"
                            b"Host: allowed.example\r\n\r\n"
                        )
                    )
                    response = bytearray()
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        response.extend(chunk)
                resolver.assert_not_called()
            finally:
                client.close()
            self.assertTrue(response.startswith(b"HTTP/1.1 403 "))
            self.assertIn(
                b"policy_outbound_budget_exceeded",
                response,
            )
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=3)

    def test_v3_connect_inner_budget_is_rejected_before_dns(self):
        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            server.ProxyHandler,
        )
        proxy_thread = threading.Thread(
            target=proxy.serve_forever,
            daemon=True,
        )
        proxy_thread.start()
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.settimeout(3)
                client.connect(proxy.server_address)
                with patch.object(server.socket, "getaddrinfo") as resolver:
                    client.sendall(
                        _policy_preface(
                            ["http://allowed.example:80"],
                            version=3,
                            limits={
                                "max_requests": 1,
                                "max_outbound_bytes": 1,
                                "max_response_wire_bytes": 1,
                            },
                        )
                        + (
                            b"CONNECT allowed.example:80 HTTP/1.1\r\n"
                            b"Host: allowed.example:80\r\n\r\n"
                        )
                    )
                    established = bytearray()
                    while b"\r\n\r\n" not in established:
                        established.extend(client.recv(4096))
                    self.assertTrue(
                        established.startswith(b"HTTP/1.1 200 ")
                    )
                    client.sendall(
                        b"GET /proof HTTP/1.1\r\n"
                        b"Host: allowed.example\r\n\r\n"
                    )
                    response = bytearray()
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        response.extend(chunk)
                resolver.assert_not_called()
            finally:
                client.close()
            self.assertTrue(response.startswith(b"HTTP/1.1 403 "))
            self.assertIn(
                b"policy_outbound_budget_exceeded",
                response,
            )
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=3)

    def test_http_host_mismatch_is_rejected_before_dns_or_upstream(self):
        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            server.ProxyHandler,
        )
        proxy_thread = threading.Thread(
            target=proxy.serve_forever,
            daemon=True,
        )
        proxy_thread.start()
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.settimeout(3)
                client.connect(
                    ("127.0.0.1", int(proxy.server_address[1]))
                )
                with patch.object(server.socket, "getaddrinfo") as resolver:
                    client.sendall(
                        _policy_preface(
                            ["http://allowed.example:80"]
                        )
                        + (
                            b"GET http://allowed.example/proof HTTP/1.1\r\n"
                            b"Host: other.example\r\n\r\n"
                        )
                    )
                    response = bytearray()
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        response.extend(chunk)
                resolver.assert_not_called()
            finally:
                client.close()
            self.assertTrue(response.startswith(b"HTTP/1.1 403 "))
            self.assertIn(b"http_host_target_mismatch", response)
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=3)

    def test_exact_url_and_method_are_rejected_before_dns(self):
        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            server.ProxyHandler,
        )
        proxy_thread = threading.Thread(
            target=proxy.serve_forever,
            daemon=True,
        )
        proxy_thread.start()
        origin = "http://allowed.example:80"
        try:
            denied_requests = (
                (
                    b"GET http://allowed.example/api/v10 HTTP/1.1\r\n"
                    b"Host: allowed.example\r\n\r\n"
                ),
                (
                    b"POST http://allowed.example/api/v1 HTTP/1.1\r\n"
                    b"Host: allowed.example\r\n"
                    b"Content-Length: 0\r\n\r\n"
                ),
            )
            for request in denied_requests:
                with self.subTest(request=request), (
                    socket.create_connection(
                        proxy.server_address,
                        timeout=3,
                    )
                ) as client:
                    client.settimeout(3)
                    with patch.object(
                        server.socket,
                        "getaddrinfo",
                    ) as resolver:
                        client.sendall(
                            _policy_preface(
                                [origin],
                                egress_rules=[{
                                    "methods": ["GET"],
                                    "url_prefix": (
                                        origin + "/api/v1"
                                    ),
                                }],
                                private_origins=[],
                            )
                            + request
                        )
                        response = bytearray()
                        while True:
                            chunk = client.recv(4096)
                            if not chunk:
                                break
                            response.extend(chunk)
                    resolver.assert_not_called()
                    self.assertTrue(
                        response.startswith(b"HTTP/1.1 403 "),
                        response,
                    )
                    self.assertIn(
                        b"request_url_not_allowed",
                        response,
                    )
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(timeout=3)

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
                    _policy_preface([
                        f"http://127.0.0.1:{origin_port}",
                    ])
                    +
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

    def test_absolute_http_waits_for_response_before_closing_request_side(
        self,
    ):
        """A complete HTTP request is not an invitation to send an early FIN.

        Some production HTTP servers treat a client write-half close before
        their response as an aborted request.  The proxy already injects
        ``Connection: close`` and permits exactly one framed request, so the
        response boundary must be owned by HTTP rather than TCP half-close.
        """

        class FinSensitiveOrigin(socketserver.BaseRequestHandler):
            def handle(self):
                request = bytearray()
                self.request.settimeout(1)
                while b"\r\n\r\n" not in request:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    request.extend(chunk)

                # Give the proxy a deterministic opportunity to expose an
                # early write-half close.  A readable EOF means the caller
                # aborted before this origin produced its response.
                self.request.settimeout(0.15)
                try:
                    premature = self.request.recv(1)
                except socket.timeout:
                    premature = None
                if premature == b"":
                    return

                body = b"fin-safe-origin"
                self.request.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 15\r\n"
                    b"Connection: close\r\n\r\n"
                    + body
                )

        origin = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0),
            FinSensitiveOrigin,
        )
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
                client.settimeout(3)
                client.sendall(
                    _policy_preface([
                        f"http://127.0.0.1:{origin_port}",
                    ])
                    +
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
            self.assertIn(b"HTTP/1.1 200 OK", response)
            self.assertTrue(response.endswith(b"fin-safe-origin"))
        finally:
            proxy.shutdown()
            origin.shutdown()
            proxy.server_close()
            origin.server_close()
            proxy_thread.join(timeout=3)
            origin_thread.join(timeout=3)

    def test_signed_http_connect_supports_node_fetch_without_opaque_tunnel(
        self,
    ):
        requests_seen: list[str] = []

        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests_seen.append(self.headers["Host"])
                body = b"node-http-connect"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
        origin_port = int(origin.server_address[1])
        origin_text = f"http://127.0.0.1:{origin_port}"

        class GrantedProxyHandler(server.ProxyHandler):
            policy = server.AddressPolicy(
                public_ports=(80, 443),
                private_origins=(origin_text,),
                private_cidrs=("127.0.0.1/32",),
            )

        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            GrantedProxyHandler,
        )
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
                proxy.server_address,
                timeout=3,
            ) as client:
                client.settimeout(3)
                client.sendall(
                    _policy_preface([origin_text])
                    + (
                        f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{origin_port}\r\n\r\n"
                    ).encode("ascii")
                )
                established = bytearray()
                while b"\r\n\r\n" not in established:
                    established.extend(client.recv(4096))
                self.assertTrue(established.startswith(
                    b"HTTP/1.1 200 Connection Established"
                ))
                client.sendall(
                    (
                        "GET /proof HTTP/1.1\r\n"
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
            self.assertTrue(response.endswith(b"node-http-connect"))
            self.assertEqual(
                [f"127.0.0.1:{origin_port}"],
                requests_seen,
            )
        finally:
            proxy.shutdown()
            origin.shutdown()
            proxy.server_close()
            origin.server_close()
            proxy_thread.join(timeout=3)
            origin_thread.join(timeout=3)

    def test_private_https_origin_uses_mitm_and_exact_spki_pin(self):
        requests_seen: list[str] = []

        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests_seen.append(self.headers["Host"])
                body = b"mitm-origin"
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

        origin = TlsOrigin(("127.0.0.1", 0), OriginHandler)
        origin.tls_context = (
            self.certificate_authority.server_context("127.0.0.1")
        )
        origin_port = int(origin.server_address[1])
        exact_origin = server.Origin(
            "https",
            "127.0.0.1",
            origin_port,
        )

        class GrantedProxyHandler(server.ProxyHandler):
            policy = server.AddressPolicy(
                public_ports=(80, 443),
                private_origins=(server._origin_text(exact_origin),),
                private_cidrs=("127.0.0.1/32",),
            )
            certificate_authority = self.certificate_authority
            upstream_tls_policy = server.UpstreamTlsPolicy({
                exact_origin: frozenset({
                    self.certificate_authority.leaf_spki,
                }),
            })

        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            GrantedProxyHandler,
        )
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
            raw = socket.create_connection(
                proxy.server_address,
                timeout=3,
            )
            raw.settimeout(3)
            raw.sendall(
                _policy_preface([server._origin_text(exact_origin)])
                + (
                    f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{origin_port}\r\n\r\n"
                ).encode("ascii")
            )
            response = bytearray()
            while b"\r\n\r\n" not in response:
                response.extend(raw.recv(4096))
            self.assertTrue(response.startswith(
                b"HTTP/1.1 200 Connection Established"
            ))
            client_context = ssl.create_default_context(
                cafile=str(
                    self.certificate_authority.public_ca_path
                )
            )
            with client_context.wrap_socket(
                raw,
                server_hostname="127.0.0.1",
            ) as client:
                client.sendall(
                    (
                        "GET /proof HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{origin_port}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                encrypted_response = bytearray()
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    encrypted_response.extend(chunk)
            self.assertIn(b"HTTP/1.0 200 OK", encrypted_response)
            self.assertTrue(encrypted_response.endswith(b"mitm-origin"))
            self.assertEqual(
                [f"127.0.0.1:{origin_port}"],
                requests_seen,
            )
        finally:
            proxy.shutdown()
            origin.shutdown()
            proxy.server_close()
            origin.server_close()
            proxy_thread.join(timeout=3)
            origin_thread.join(timeout=3)

    @unittest.skipUnless(
        ssl.HAS_TLSv1_3
        and hasattr(ssl.SSLContext, "post_handshake_auth"),
        "TLS 1.3 post-handshake authentication is unavailable",
    )
    def test_upstream_tls_clienthello_advertises_post_handshake_auth(
        self,
    ):
        host = "pha-required.example"
        statuses: list[int] = []

        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    self.request.verify_client_post_handshake()
                except ssl.SSLError:
                    status = 403
                    body = b"post-handshake-auth-extension-required"
                else:
                    status = 200
                    body = b"post-handshake-auth-extension-present"
                statuses.append(status)
                self.send_response(status)
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

        origin = TlsOrigin(("127.0.0.1", 0), OriginHandler)
        origin_context = self.certificate_authority.server_context(
            host
        )
        origin_context.minimum_version = ssl.TLSVersion.TLSv1_3
        origin_context.maximum_version = ssl.TLSVersion.TLSv1_3
        origin_context.post_handshake_auth = True
        origin_context.verify_mode = ssl.CERT_OPTIONAL
        origin.tls_context = origin_context
        origin_port = int(origin.server_address[1])
        exact_origin = server.Origin("https", host, origin_port)
        destination = server.Destination(
            "https",
            host,
            origin_port,
            "127.0.0.1",
            socket.AF_INET,
            False,
        )
        origin_thread = threading.Thread(
            target=origin.serve_forever,
            daemon=True,
        )
        origin_thread.start()

        def trusted_default_context(*, purpose):
            self.assertEqual(ssl.Purpose.SERVER_AUTH, purpose)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(
                cafile=str(
                    self.certificate_authority.public_ca_path
                )
            )
            return context

        try:
            policies = (
                (
                    "public-ca",
                    server.UpstreamTlsPolicy({}),
                    patch.object(
                        server.ssl,
                        "create_default_context",
                        side_effect=trusted_default_context,
                    ),
                    ssl.CERT_REQUIRED,
                    True,
                ),
                (
                    "exact-spki-pin",
                    server.UpstreamTlsPolicy({
                        exact_origin: frozenset({
                            self.certificate_authority.leaf_spki,
                        }),
                    }),
                    patch.object(
                        server.ssl,
                        "create_default_context",
                    ),
                    ssl.CERT_NONE,
                    False,
                ),
            )
            for (
                lane,
                policy,
                context_factory_patch,
                expected_verify_mode,
                expected_check_hostname,
            ) in policies:
                with self.subTest(lane=lane), context_factory_patch:
                    raw = socket.create_connection(
                        origin.server_address,
                        timeout=3,
                    )
                    raw.settimeout(3)
                    with policy.wrap(raw, destination) as upstream:
                        self.assertTrue(
                            upstream.context.post_handshake_auth
                        )
                        self.assertEqual(
                            expected_verify_mode,
                            upstream.context.verify_mode,
                        )
                        self.assertEqual(
                            expected_check_hostname,
                            upstream.context.check_hostname,
                        )
                        self.assertEqual(
                            ssl.TLSVersion.TLSv1_2,
                            upstream.context.minimum_version,
                        )
                        self.assertEqual(
                            "http/1.1",
                            upstream.selected_alpn_protocol(),
                        )
                        upstream.sendall(
                            (
                                "GET /proof HTTP/1.1\r\n"
                                f"Host: {host}:{origin_port}\r\n"
                                "Connection: close\r\n\r\n"
                            ).encode("ascii")
                        )
                        response = bytearray()
                        while True:
                            chunk = upstream.recv(4096)
                            if not chunk:
                                break
                            response.extend(chunk)
                    self.assertTrue(
                        response.startswith(b"HTTP/1.0 200 OK")
                    )
                    self.assertTrue(response.endswith(
                        b"post-handshake-auth-extension-present"
                    ))
            self.assertEqual([200, 200], statuses)
        finally:
            origin.shutdown()
            origin.server_close()
            origin_thread.join(timeout=3)

    def test_https_domain_fronting_is_rejected_before_upstream(self):
        requests_seen: list[str] = []

        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests_seen.append(self.headers["Host"])
                self.send_response(204)
                self.send_header("Connection", "close")
                self.end_headers()

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

        origin = TlsOrigin(("127.0.0.1", 0), OriginHandler)
        origin.tls_context = (
            self.certificate_authority.server_context(
                "allowed.example"
            )
        )
        origin_port = int(origin.server_address[1])
        exact_origin = server.Origin(
            "https",
            "allowed.example",
            origin_port,
        )

        class GrantedProxyHandler(server.ProxyHandler):
            policy = server.AddressPolicy(
                public_ports=(80, 443),
                private_origins=(server._origin_text(exact_origin),),
                private_cidrs=("127.0.0.1/32",),
            )
            certificate_authority = self.certificate_authority
            upstream_tls_policy = server.UpstreamTlsPolicy({
                exact_origin: frozenset({
                    self.certificate_authority.leaf_spki,
                }),
            })

        proxy = server.ThreadingProxyServer(
            ("127.0.0.1", 0),
            GrantedProxyHandler,
        )
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
        records = [(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("127.0.0.1", origin_port),
        )]
        try:
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.settimeout(3)
            raw.connect(proxy.server_address)
            with patch.object(
                server.socket,
                "getaddrinfo",
                return_value=records,
            ):
                raw.sendall(
                    _policy_preface([
                        server._origin_text(exact_origin),
                    ])
                    + (
                        f"CONNECT allowed.example:{origin_port} HTTP/1.1\r\n"
                        f"Host: allowed.example:{origin_port}\r\n\r\n"
                    ).encode("ascii")
                )
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    response.extend(raw.recv(4096))
                self.assertTrue(response.startswith(
                    b"HTTP/1.1 200 Connection Established"
                ))
                # Even a raw client which disables certificate verification
                # cannot use a matching outer SNI as a domain-fronting tunnel.
                client_context = ssl._create_unverified_context()
                with client_context.wrap_socket(
                    raw,
                    server_hostname="allowed.example",
                ) as client:
                    client.sendall(
                        b"GET /secret HTTP/1.1\r\n"
                        b"Host: forbidden.example\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                    denied = bytearray()
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        denied.extend(chunk)
            self.assertTrue(denied.startswith(b"HTTP/1.1 403 "))
            self.assertIn(b"https_host_origin_mismatch", denied)
            self.assertEqual([], requests_seen)
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
