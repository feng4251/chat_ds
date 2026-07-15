import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
import tools.code_execution as code_execution
import tools.path_security as path_security
from agent_loop import (
    HarnessRunState,
    _compact_tool_call_arguments,
    _deterministic_verifier_payload,
)
from tools.code_execution import _code_with_session_snapshot


class ToolArgumentAndReportQualityTests(unittest.TestCase):
    def test_compacted_write_file_history_does_not_emit_copyable_placeholder(self):
        args = json.dumps({"filepath": "report.md", "content": "x" * 2000})

        compacted = _compact_tool_call_arguments("write_file", args)

        self.assertNotIn("__CHATDS_OMITTED", compacted)
        payload = json.loads(compacted)
        self.assertTrue(payload["content"]["_chatds_argument_omitted"])
        self.assertEqual(payload["content"]["kind"], "large_file_content")

    def test_execute_code_skips_snapshot_when_workspace_snapshot_is_large(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(path_security, "SANDBOX_ROOT", root), patch.object(code_execution, "SANDBOX_ROOT", root):
                workspace = path_security.sandbox_dir("u", "s", sub="workspace")
                for index in range(5):
                    (workspace / f"large_{index}.md").write_text("x" * 180_000, encoding="utf-8")

                code = _code_with_session_snapshot("print('ok')", "u", "s")

                self.assertIn("skipped automatic session snapshot injection", code)
                self.assertLess(len(code.encode("utf-8")), 900_000)

    def test_skill_report_verifier_rejects_duplicate_numbered_headings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                report = workspace / "FULL_REPORT.md"
                report.write_text(
                    "# 1. A\n"
                    "## 1.1 First\ncontent\n"
                    "## 1.1 Duplicate\ncontent\n"
                    "# 2. B\n## 2.1 Next\ncontent\n",
                    encoding="utf-8",
                )
                run_state = HarnessRunState(user_id="u", session_id="s")
                run_state.artifacts.append({
                    "kind": "file",
                    "path": "FULL_REPORT.md",
                    "size_bytes": report.stat().st_size,
                })
                run_state.skill_workflow_contracts["demo"] = {
                    "requires_merge": True,
                    "output_contract": {
                        "declared_final_artifact": "FULL_REPORT.md",
                        "expected_min_bytes": 10,
                        "expected_min_lines": 5,
                        "declared_section_count": 2,
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

                self.assertEqual(payload["verdict"], "fail")
                self.assertIn("duplicate numbered section headings", payload["reason"])


if __name__ == "__main__":
    unittest.main()
