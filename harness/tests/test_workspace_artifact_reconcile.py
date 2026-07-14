import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
from agent_loop import (
    HarnessRunState,
    _has_merged_artifact_for_contract,
    _markdown_quality_findings,
    _reconcile_workspace_artifacts,
)


class WorkspaceArtifactReconcileTests(unittest.TestCase):
    def test_reconciles_skill_declared_cat_artifact_and_verifier_passes(self):
        contract = {
            "requires_merge": True,
            "requires_modular_artifacts": True,
            "output_contract": {
                "declared_final_artifact": "{TRIAL_NAME}_FULL_REPORT.md",
                "declared_modular_files": ["01_intro.md", "02_methods.md"],
                "declared_file_count": 3,
                "declared_section_count": 2,
                "expected_min_bytes": 100,
                "expected_min_lines": 5,
                "merge_mandatory": True,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "01_intro.md").write_text("# Intro\ncontent\n", encoding="utf-8")
                (workspace / "02_methods.md").write_text("# Methods\ncontent\n", encoding="utf-8")
                final = workspace / "GAL3_AD_FULL_REPORT.md"
                final.write_text(
                    "# Intro\nline\n## Methods\nline\nline\nline\n" + ("evidence " * 20),
                    encoding="utf-8",
                )

                run_state = HarnessRunState(user_id="u", session_id="s")
                run_state.skill_workflow_contracts["healthsim-trialsim"] = contract

                payloads = _reconcile_workspace_artifacts(run_state, [contract])
                paths = {payload["path"] for payload in payloads}

                self.assertIn("01_intro.md", paths)
                self.assertIn("02_methods.md", paths)
                self.assertIn("GAL3_AD_FULL_REPORT.md", paths)
                self.assertTrue(
                    any(payload.get("is_final_report") for payload in payloads)
                )
                self.assertTrue(_has_merged_artifact_for_contract(run_state, contract))
                self.assertEqual(
                    [],
                    _markdown_quality_findings(
                        run_state,
                        ["GAL3_AD_FULL_REPORT.md"],
                        complex_report=True,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
