import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
from agent_loop import HarnessRunState, _markdown_quality_findings


class NoContractFallbackTests(unittest.TestCase):
    def test_skill_without_output_contract_gets_no_density_thresholds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "short.md").write_text("# Short\nbrief\n", encoding="utf-8")

                run_state = HarnessRunState(user_id="u", session_id="s")
                run_state.session_skill_names.add("simple-skill")
                run_state.viewed_skill_names.add("simple-skill")
                run_state.skill_workflow_contracts["simple-skill"] = {}

                self.assertEqual(
                    [],
                    _markdown_quality_findings(
                        run_state,
                        ["short.md"],
                        complex_report=True,
                    ),
                )

    def test_no_skill_complex_report_still_uses_legacy_density(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "short.md").write_text("# Short\nbrief\n", encoding="utf-8")

                run_state = HarnessRunState(user_id="u", session_id="s")
                findings = _markdown_quality_findings(
                    run_state,
                    ["short.md"],
                    complex_report=True,
                )
                self.assertTrue(findings)
                self.assertIn("complex deliverable", findings[0])


if __name__ == "__main__":
    unittest.main()
