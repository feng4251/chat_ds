import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_runner import mcp_process


class ProcessMcpTests(unittest.TestCase):
    def test_typed_process_lifecycle_supports_persistent_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            executable = Path(sys.executable).resolve()
            with (
                patch.object(mcp_process, "WORKING_ROOTS", (root,)),
                patch.object(
                    mcp_process,
                    "EXECUTABLE_ROOTS",
                    (executable.parent,),
                ),
            ):
                registry = mcp_process.ProcessRegistry()
                try:
                    opened = registry.open([
                        str(executable),
                        "-u",
                        "-c",
                        (
                            "import sys; "
                            "[(print('ack:'+line.strip(), flush=True)) "
                            "for line in sys.stdin]"
                        ),
                    ], str(root))
                    process_id = opened["process_id"]
                    registry.write(process_id, "fixture")
                    deadline = time.monotonic() + 2
                    observed = ""
                    while time.monotonic() < deadline:
                        observed = registry.read(process_id)["output"]
                        if "ack:fixture" in observed:
                            break
                        time.sleep(0.02)
                    self.assertIn("ack:fixture", observed)
                    closed = registry.close(process_id)
                    self.assertEqual(closed["status"], "exited")
                finally:
                    registry.cleanup()

    def test_paths_and_executables_are_bounded(self):
        registry = mcp_process.ProcessRegistry()
        try:
            with self.assertRaisesRegex(
                mcp_process.ProcessToolError, "process_cwd_invalid"
            ):
                registry.open(["/usr/bin/python3", "-c", "pass"], "/tmp")
            with self.assertRaisesRegex(
                mcp_process.ProcessToolError, "process_executable_invalid"
            ):
                registry.open(["/workspace/not-an-executable"])
        finally:
            registry.cleanup()

    def test_child_environment_removes_provider_credentials(self):
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "fixture-secret",
            "UNRELATED_API_TOKEN": "fixture-secret",
            "HTTP_PROXY": "http://127.0.0.1:8080",
        }, clear=True):
            child = mcp_process._child_environment()
        self.assertNotIn("ANTHROPIC_API_KEY", child)
        self.assertNotIn("UNRELATED_API_TOKEN", child)
        self.assertEqual(child["HTTP_PROXY"], "http://127.0.0.1:8080")

    def test_tool_inventory_is_small_and_typed(self):
        self.assertEqual(
            [tool["name"] for tool in mcp_process.TOOLS],
            ["process_open", "process_write", "process_read", "process_close"],
        )
        self.assertTrue(all(
            tool["inputSchema"]["additionalProperties"] is False
            for tool in mcp_process.TOOLS
        ))


if __name__ == "__main__":
    unittest.main()
