import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from claude_runner import bootstrap


class RunnerBootstrapTests(unittest.TestCase):
    def test_import_failure_is_a_stable_bounded_fatal(self):
        def fail(_module, *, run_name):
            self.assertEqual(run_name, "__main__")
            raise ModuleNotFoundError("fixture path and implementation detail")

        stderr = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stderr(stderr):
            code = bootstrap.main([], module_runner=fail)
        self.assertEqual(code, 70)
        self.assertEqual(json.loads(stderr.getvalue()), {
            "type": "chatds.runner.fatal",
            "code": "runner_runtime_import_failed",
        })
        self.assertNotIn("fixture", stderr.getvalue())

    def test_bootstrap_failure_writes_one_machine_owned_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            with patch.object(
                bootstrap,
                "_RUN_LEDGER",
                re.compile("^" + re.escape(path.as_posix()) + "$"),
            ):
                self.assertTrue(bootstrap._append_bootstrap_terminal(
                    path,
                    code="runner_runtime_import_failed",
                ))
                self.assertFalse(bootstrap._append_bootstrap_terminal(
                    path,
                    code="runner_runtime_import_failed",
                ))
            envelope = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(envelope["channel"], "bootstrap")
        self.assertEqual(envelope["event"]["status"], "failed")
        self.assertEqual(
            envelope["event"]["error_code"],
            "runner_runtime_import_failed",
        )
        self.assertEqual(envelope["event"]["error_stage"], "bootstrap_import")
        self.assertEqual(envelope["event"]["exit_code"], 70)

    def test_self_test_uses_a_distinct_fast_path(self):
        seen = []

        def complete(module, *, run_name):
            seen.append((module, run_name))
            raise SystemExit(0)

        self.assertEqual(bootstrap.main(
            [bootstrap.IMAGE_SELF_TEST_ARGUMENT],
            module_runner=complete,
        ), 0)
        self.assertEqual(
            seen,
            [("claude_runner.image_selftest", "__main__")],
        )


if __name__ == "__main__":
    unittest.main()
