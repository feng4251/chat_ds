from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import mcp_client


class MCPStdioSandboxTests(unittest.TestCase):
    def test_launcher_environment_cannot_execute_child_hooks_as_root(self) -> None:
        child_env = {
            "PATH": "/session/venv/bin:/usr/bin",
            "PYTHONPATH": "/skill/scripts",
            "LD_PRELOAD": "/tmp/untrusted.so",
            "API_KEY": "explicit-secret",
        }
        with patch.object(mcp_client.secrets, "token_hex", return_value="abc123"):
            command, args, launcher_env, sandbox_home = mcp_client._sandboxed_stdio_parameters(
                "/session/venv/bin/python",
                ["/skill/server.py"],
                child_env,
            )

        self.assertEqual(command, mcp_client.sys.executable)
        self.assertEqual(args[:3], ["-I", str(mcp_client.MCP_STDIO_SANDBOX_LAUNCHER), "--"])
        self.assertNotIn("PYTHONPATH", launcher_env)
        self.assertNotIn("LD_PRELOAD", launcher_env)
        self.assertNotIn("API_KEY", launcher_env)
        decoded = json.loads(base64.urlsafe_b64decode(
            launcher_env[mcp_client.MCP_STDIO_CHILD_SPEC_ENV]
        ))
        self.assertEqual(decoded["env"]["LD_PRELOAD"], "/tmp/untrusted.so")
        self.assertEqual(decoded["env"]["HOME"], "/tmp/chatds-mcp-abc123")
        self.assertEqual(sandbox_home.as_posix(), "/tmp/chatds-mcp-abc123")

    def test_ambient_harness_secret_is_not_in_launcher_or_child_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "INTERNAL_API_TOKEN": "must-not-leak",
                "PYTHONSTARTUP": "/tmp/hook.py",
            },
            clear=True,
        ):
            _, _, launcher_env, _ = mcp_client._sandboxed_stdio_parameters(
                "/usr/bin/server", [], mcp_client._build_safe_env(None)
            )
        decoded = json.loads(base64.urlsafe_b64decode(
            launcher_env[mcp_client.MCP_STDIO_CHILD_SPEC_ENV]
        ))
        self.assertNotIn("INTERNAL_API_TOKEN", repr(launcher_env))
        self.assertNotIn("INTERNAL_API_TOKEN", decoded["env"])
        self.assertNotIn("PYTHONSTARTUP", decoded["env"])

    def test_environment_payload_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds"):
            mcp_client._sandboxed_stdio_parameters(
                "/usr/bin/server",
                [],
                {"BIG": "x" * mcp_client.MCP_STDIO_CHILD_SPEC_MAX_BYTES},
            )

    def test_runtime_home_cleanup_does_not_follow_replacement_symlink(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as outer:
            root = Path(outer)
            target = root / "keep"
            target.mkdir()
            sentinel = target / "sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            replacement = Path("/tmp") / f"chatds-mcp-{root.name}"
            replacement.symlink_to(target, target_is_directory=True)
            self.addCleanup(lambda: replacement.unlink(missing_ok=True))

            mcp_client._remove_stdio_sandbox_home(replacement)

            self.assertFalse(replacement.exists())
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.geteuid() == 0, "privilege transition requires root")
    def test_real_launcher_drops_identity_and_ambient_secret(self) -> None:
        probe = (
            "import json,os; "
            "s=open('/proc/self/status',encoding='utf-8').read(); "
            "print(json.dumps({'uid':os.geteuid(),"
            "'token':os.environ.get('INTERNAL_API_TOKEN'),"
            "'nnp':'NoNewPrivs:\\t1' in s}))"
        )
        command, args, launcher_env, sandbox_home = (
            mcp_client._sandboxed_stdio_parameters(
                sys.executable,
                ["-I", "-c", probe],
                {"PATH": os.environ.get("PATH", "/usr/bin")},
            )
        )
        launcher_env["INTERNAL_API_TOKEN"] = "ambient-must-not-survive"
        try:
            completed = subprocess.run(
                [command, *args],
                env=launcher_env,
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            result = json.loads(completed.stdout)
            self.assertEqual(65534, result["uid"])
            self.assertIsNone(result["token"])
            self.assertTrue(result["nnp"])
        finally:
            mcp_client._remove_stdio_sandbox_home(sandbox_home)


if __name__ == "__main__":
    unittest.main()
