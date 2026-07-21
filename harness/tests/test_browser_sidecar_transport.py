from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import tools.browser as browser_tools
from tools.browser import _UnixSocketTcpRelay, _get_browser


_SIDECAR_BROWSER_PATH = (
    "/devtools/browser/12345678-abcd-4abc-8abc-1234567890ab"
)


def _websocket_handshake(path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Upgrade: websocket\r\n"
        "Connection: keep-alive, Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "\r\n"
    ).encode("ascii")


class BrowserUnixRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_binds_loopback_and_rewrites_tokenized_handshake(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "cdp.sock"
            sidecar_requests = []

            async def echo(reader, writer):
                payload = await reader.read(4096)
                sidecar_requests.append(payload)
                if payload.startswith(b"GET /json/version "):
                    body = json.dumps(
                        {
                            "webSocketDebuggerUrl": (
                                f"ws://127.0.0.1:49152{_SIDECAR_BROWSER_PATH}"
                            )
                        }
                    ).encode("utf-8")
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n".encode("ascii")
                        + b"Connection: close\r\n\r\n"
                        + body
                    )
                else:
                    writer.write(b"sidecar-handshake-received")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            unix_server = await asyncio.start_unix_server(
                echo, path=str(socket_path)
            )
            relay = _UnixSocketTcpRelay(str(socket_path))
            self.addAsyncCleanup(relay.close)
            self.addAsyncCleanup(self._close_server, unix_server)

            endpoint = await relay.start()
            parsed = urlsplit(endpoint)
            self.assertEqual("ws", parsed.scheme)
            self.assertEqual("127.0.0.1", parsed.hostname)
            self.assertIsNone(parsed.username)
            self.assertIsNotNone(parsed.port)
            self.assertTrue(parsed.path.startswith("/__chatds_cdp__/"))
            self.assertTrue(parsed.path.endswith(_SIDECAR_BROWSER_PATH))
            relay_token = parsed.path[
                len("/__chatds_cdp__/"):-len(_SIDECAR_BROWSER_PATH)
            ]
            self.assertRegex(relay_token, r"^[A-Za-z0-9_-]{40,64}$")
            self.assertNotEqual(_SIDECAR_BROWSER_PATH, parsed.path)

            reader, writer = await asyncio.open_connection(
                parsed.hostname, parsed.port
            )
            writer.write(_websocket_handshake(parsed.path))
            await writer.drain()
            self.assertEqual(b"sidecar-handshake-received", await reader.read())
            writer.close()
            await writer.wait_closed()
            self.assertEqual(2, len(sidecar_requests))
            self.assertTrue(
                sidecar_requests[1].startswith(
                    f"GET {_SIDECAR_BROWSER_PATH} HTTP/1.1\r\n".encode("ascii")
                )
            )
            self.assertNotIn(b"__chatds_cdp__", sidecar_requests[1])

    async def test_unauthorized_loopback_requests_never_open_unix_socket(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "cdp.sock"
            sidecar_requests = []

            async def sidecar(reader, writer):
                payload = await reader.read(4096)
                sidecar_requests.append(payload)
                body = json.dumps(
                    {
                        "webSocketDebuggerUrl": (
                            f"ws://127.0.0.1:49152{_SIDECAR_BROWSER_PATH}"
                        )
                    }
                ).encode("utf-8")
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                    + body
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            unix_server = await asyncio.start_unix_server(
                sidecar, path=str(socket_path)
            )
            relay = _UnixSocketTcpRelay(str(socket_path))
            self.addAsyncCleanup(relay.close)
            self.addAsyncCleanup(self._close_server, unix_server)

            endpoint = urlsplit(await relay.start())
            self.assertEqual(1, len(sidecar_requests))  # discovery only
            for unauthorized_path in (
                _SIDECAR_BROWSER_PATH,
                "/json/version",
                endpoint.path + "?extra=1",
            ):
                reader, writer = await asyncio.open_connection(
                    endpoint.hostname, endpoint.port
                )
                writer.write(_websocket_handshake(unauthorized_path))
                await writer.drain()
                response = await reader.read()
                self.assertTrue(response.startswith(b"HTTP/1.1 404 Not Found"))
                writer.close()
                await writer.wait_closed()

            await asyncio.sleep(0)
            self.assertEqual(1, len(sidecar_requests))

    @staticmethod
    async def _close_server(server):
        server.close()
        await server.wait_closed()

    async def test_missing_or_non_socket_control_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = _UnixSocketTcpRelay(str(Path(temp_dir) / "missing.sock"))
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                await missing.start()

            ordinary_file = Path(temp_dir) / "ordinary-file"
            ordinary_file.write_text("not a socket", encoding="utf-8")
            wrong_type = _UnixSocketTcpRelay(str(ordinary_file))
            with self.assertRaisesRegex(RuntimeError, "not a Unix socket"):
                await wrong_type.start()

            symlink = Path(temp_dir) / "socket-link"
            unix_server = await asyncio.start_unix_server(
                lambda _reader, _writer: None,
                path=str(Path(temp_dir) / "real.sock"),
            )
            self.addAsyncCleanup(self._close_server, unix_server)
            symlink.symlink_to(Path(temp_dir) / "real.sock")
            linked = _UnixSocketTcpRelay(str(symlink))
            with self.assertRaisesRegex(RuntimeError, "not a Unix socket"):
                await linked.start()

    async def test_discovery_rejects_sidecar_local_authority_escape(self):
        async def unsafe_discovery(reader, writer):
            await reader.read(1024)
            body = json.dumps(
                {
                    "webSocketDebuggerUrl": (
                        "ws://attacker.example/devtools/browser/escape?token=bad"
                    )
                }
            ).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "cdp.sock"
            unix_server = await asyncio.start_unix_server(
                unsafe_discovery, path=str(socket_path)
            )
            self.addAsyncCleanup(self._close_server, unix_server)
            relay = _UnixSocketTcpRelay(str(socket_path))
            self.addAsyncCleanup(relay.close)
            with self.assertRaisesRegex(RuntimeError, "endpoint is unsafe"):
                await relay.start()


class _FakeBrowser:
    def __init__(self, connected=True):
        self.connected = connected
        self.closed = False

    def is_connected(self):
        return self.connected

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, error=None):
        self.connected_endpoint = None
        self.connected_timeout = None
        self.error = error

    async def connect_over_cdp(self, endpoint, *, timeout):
        self.connected_endpoint = endpoint
        self.connected_timeout = timeout
        if self.error is not None:
            raise self.error
        return _FakeBrowser()

    async def launch(self, **_kwargs):  # pragma: no cover - safety tripwire
        raise AssertionError("local Chromium launch must never be called")


class _FakePlaywright:
    def __init__(self, error=None):
        self.chromium = _FakeChromium(error=error)
        self.stopped = False

    async def stop(self):
        self.stopped = True


class _FakeStarter:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


class _FakeRelay:
    instances = []

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.closed = False
        self.__class__.instances.append(self)

    async def start(self):
        return "ws://127.0.0.1:43210/devtools/browser/test-browser"

    async def close(self):
        self.closed = True


class BrowserSidecarConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = (
            browser_tools._browser_instance,
            browser_tools._playwright_instance,
            browser_tools._browser_relay,
        )
        browser_tools._browser_instance = None
        browser_tools._playwright_instance = None
        browser_tools._browser_relay = None
        _FakeRelay.instances.clear()

    async def asyncTearDown(self):
        browser_tools._browser_instance = None
        browser_tools._playwright_instance = None
        browser_tools._browser_relay = None
        (
            browser_tools._browser_instance,
            browser_tools._playwright_instance,
            browser_tools._browser_relay,
        ) = self.saved

    async def test_get_browser_uses_only_connect_over_cdp(self):
        fake_pw = _FakePlaywright()
        with (
            patch(
                "playwright.async_api.async_playwright",
                return_value=_FakeStarter(fake_pw),
            ),
            patch("tools.browser._UnixSocketTcpRelay", _FakeRelay),
            patch.object(
                browser_tools.settings,
                "browser_cdp_socket",
                "/run/chat-ds-browser/cdp.sock",
            ),
            patch.object(
                browser_tools.settings,
                "browser_cdp_connect_timeout_seconds",
                7.5,
            ),
        ):
            browser = await _get_browser()

        self.assertIsInstance(browser, _FakeBrowser)
        self.assertEqual(
            "ws://127.0.0.1:43210/devtools/browser/test-browser",
            fake_pw.chromium.connected_endpoint,
        )
        self.assertEqual(7_500, fake_pw.chromium.connected_timeout)
        self.assertEqual(
            "/run/chat-ds-browser/cdp.sock",
            _FakeRelay.instances[0].socket_path,
        )
        self.assertFalse(fake_pw.stopped)

    async def test_connection_failure_closes_driver_and_relay(self):
        fake_pw = _FakePlaywright(error=RuntimeError("CDP unavailable"))
        with (
            patch(
                "playwright.async_api.async_playwright",
                return_value=_FakeStarter(fake_pw),
            ),
            patch("tools.browser._UnixSocketTcpRelay", _FakeRelay),
            patch.object(
                browser_tools.settings,
                "browser_cdp_socket",
                "/run/chat-ds-browser/cdp.sock",
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "local launch is disabled"):
                await _get_browser()

        self.assertTrue(fake_pw.stopped)
        self.assertTrue(_FakeRelay.instances[0].closed)
        self.assertIsNone(browser_tools._browser_instance)
        self.assertIsNone(browser_tools._playwright_instance)
        self.assertIsNone(browser_tools._browser_relay)

    async def test_disconnected_transport_is_replaced_and_sessions_invalidated(self):
        old_browser = _FakeBrowser(connected=False)
        old_playwright = _FakePlaywright()
        old_relay = _FakeRelay("/old.sock")
        browser_tools._browser_instance = old_browser
        browser_tools._playwright_instance = old_playwright
        browser_tools._browser_relay = old_relay
        browser_tools._sessions[("user", "session", "run")] = {
            "context": None,
            "browser": old_browser,
        }
        browser_tools._last_activity[("user", "session", "run")] = 1.0

        fresh_pw = _FakePlaywright()
        with (
            patch(
                "playwright.async_api.async_playwright",
                return_value=_FakeStarter(fresh_pw),
            ),
            patch("tools.browser._UnixSocketTcpRelay", _FakeRelay),
            patch.object(
                browser_tools.settings,
                "browser_cdp_socket",
                "/run/chat-ds-browser/cdp.sock",
            ),
        ):
            fresh_browser = await _get_browser()

        self.assertIsNot(old_browser, fresh_browser)
        self.assertTrue(old_browser.closed)
        self.assertTrue(old_playwright.stopped)
        self.assertTrue(old_relay.closed)
        self.assertEqual({}, browser_tools._sessions)
        self.assertEqual({}, browser_tools._last_activity)


if __name__ == "__main__":
    unittest.main()
