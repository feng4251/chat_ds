from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, call, patch

from browser_sidecar import server


class BrowserRuntimeDirectoryTests(unittest.TestCase):
    def _prepare(self, root: Path) -> Path:
        profile = root / "profile"
        socket_path = root / "run" / "cdp.sock"
        environment = {
            "HOME": str(root / "home"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
        }
        with (
            patch.object(server, "PROFILE_DIR", profile),
            patch.object(
                server,
                "ACTIVE_PORT_FILE",
                profile / "DevToolsActivePort",
            ),
            patch.object(server, "SOCKET_PATH", socket_path),
            patch.dict(os.environ, environment),
        ):
            server._prepare_runtime_dirs()
        return profile

    def test_startup_removes_stale_profile_and_active_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "profile"
            profile.mkdir()
            (profile / "DevToolsActivePort").write_text(
                "12345\n/devtools/browser/stale\n",
                encoding="utf-8",
            )
            (profile / "SingletonLock").write_text("stale", encoding="utf-8")

            prepared = self._prepare(root)

            self.assertEqual(profile, prepared)
            self.assertEqual([], list(profile.iterdir()))
            self.assertEqual(
                0o700,
                stat.S_IMODE(profile.stat().st_mode),
            )
            self.assertEqual(
                0o700,
                stat.S_IMODE((root / "run").stat().st_mode),
            )

    def test_profile_symlink_is_replaced_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            (root / "profile").symlink_to(outside, target_is_directory=True)

            prepared = self._prepare(root)

            self.assertFalse(prepared.is_symlink())
            self.assertTrue(prepared.is_dir())
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    def test_chromium_disables_quic_and_nonproxied_webrtc_udp(self):
        command = server._chromium_command()

        self.assertIn("--disable-quic", command)
        self.assertIn(
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            command,
        )

    def test_chromium_uses_only_valid_certificate_spki_exceptions(self):
        first = "A" * 43 + "="
        second = "b" * 43
        with patch.dict(
            os.environ,
            {"BROWSER_TLS_SPKI_ALLOWLIST": f"{first}, {second}, {first}"},
        ):
            command = server._chromium_command()

        self.assertIn(
            "--ignore-certificate-errors-spki-list="
            + first
            + ","
            + second,
            command,
        )
        self.assertNotIn("--ignore-certificate-errors", command)

    def test_malformed_certificate_spki_exception_fails_closed(self):
        with patch.dict(
            os.environ,
            {"BROWSER_TLS_SPKI_ALLOWLIST": "not-a-valid-spki-hash"},
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "BROWSER_TLS_SPKI_ALLOWLIST",
            ):
                server._chromium_command()

    def test_http_health_response_is_complete_without_connection_eof(self):
        body = b'{"webSocketDebuggerUrl":"ws://localhost/devtools/browser/id"}'
        response = (
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length:{len(body)}\r\n".encode("ascii")
            + b"Content-Type:application/json\r\n\r\n"
            + body
        )

        self.assertTrue(server._http_response_has_complete_body(response))
        self.assertFalse(server._http_response_has_complete_body(response[:-1]))


class BrowserProcessCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_exited_leader_still_signals_remaining_process_group(self):
        process = type("Process", (), {})()
        process.pid = 43210
        process.returncode = 9
        process.wait = AsyncMock()

        with patch.object(
            server.os,
            "killpg",
            side_effect=(None, ProcessLookupError()),
        ) as killpg:
            await server._terminate_process_group(process)

        self.assertEqual(
            [
                call(43210, server.signal.SIGTERM),
                call(43210, 0),
            ],
            killpg.call_args_list,
        )
        process.wait.assert_not_awaited()

    async def test_log_drain_is_bounded_when_pipe_never_closes(self):
        never = asyncio.Event()
        log_task = asyncio.create_task(never.wait())
        with patch.object(server, "LOG_DRAIN_TIMEOUT_SECONDS", 0.01):
            await server._finish_log_task(log_task)
        self.assertTrue(log_task.cancelled())


if __name__ == "__main__":
    unittest.main()
