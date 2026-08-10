import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CLAUDE_BINARY = Path("/usr/local/bin/claude")


@unittest.skipUnless(
    CLAUDE_BINARY.is_file(),
    "native Claude binary is only present in the Turn image",
)
class NativeContextAliasTests(unittest.TestCase):
    def test_one_million_marker_is_removed_from_wire_model(self):
        observed_models: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                model = payload.get("model")
                if isinstance(model, str):
                    observed_models.append(model)
                body = json.dumps({
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "bounded fixture stop",
                    },
                }).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                environment = {
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": temporary,
                    "ANTHROPIC_API_KEY": "fixture-key",
                    "ANTHROPIC_BASE_URL": (
                        f"http://127.0.0.1:{server.server_port}"
                    ),
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "DISABLE_TELEMETRY": "1",
                    "DISABLE_ERROR_REPORTING": "1",
                    "DISABLE_AUTOUPDATER": "1",
                }
                completed = subprocess.run(
                    [
                        str(CLAUDE_BINARY),
                        "--print",
                        "--verbose",
                        "--output-format",
                        "stream-json",
                        "--model",
                        "renamed-model-holdout[1m]",
                        "--setting-sources",
                        "",
                        "--tools",
                        "default",
                        "--permission-mode",
                        "bypassPermissions",
                        "--dangerously-skip-permissions",
                    ],
                    input=b"fixture request",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=temporary,
                    env=environment,
                    timeout=30,
                    check=False,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertTrue(
            observed_models,
            msg=(
                f"native CLI made no model request (exit={completed.returncode}, "
                f"stderr={completed.stderr.decode('utf-8', 'replace')[:500]!r})"
            ),
        )
        self.assertIn("renamed-model-holdout", observed_models)
        self.assertFalse(any("[1m]" in model for model in observed_models))


if __name__ == "__main__":
    unittest.main()
