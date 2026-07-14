import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
from agent_loop import HarnessRunState, _deterministic_verifier_payload


class VerifierFailurePayloadTests(unittest.TestCase):
    def test_failed_artifact_contract_sets_needs_more_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "GAL3_AD_FULL_REPORT.md").write_text(
                    "# Too short\nbrief\n",
                    encoding="utf-8",
                )
                run_state = HarnessRunState(user_id="u", session_id="s")
                run_state.artifacts.append({
                    "kind": "file",
                    "path": "GAL3_AD_FULL_REPORT.md",
                    "size_bytes": 18,
                })
                run_state.skill_workflow_contracts["healthsim-trialsim"] = {
                    "requires_merge": True,
                    "output_contract": {
                        "declared_final_artifact": "{TRIAL_NAME}_FULL_REPORT.md",
                        "expected_min_bytes": 1000,
                        "expected_min_lines": 100,
                        "declared_section_count": 5,
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

                self.assertEqual(payload["verdict"], "fail")
                self.assertTrue(payload["needs_more_work"])
                self.assertIn("skill's own declared completion contract", payload["reason"])

    def test_resolved_placeholder_history_does_not_block_contract_pass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                workspace.mkdir(parents=True, exist_ok=True)
                body = "\n".join(
                    ["# Galectin-3 AD Full Report"]
                    + [f"## Section {idx}\ncontent" for idx in range(1, 6)]
                )
                report = workspace / "GAL3_AD_FULL_REPORT.md"
                report.write_text(body, encoding="utf-8")
                run_state = HarnessRunState(user_id="u", session_id="s")
                run_state.invalid_placeholder_write_count = 1
                run_state.invalid_placeholder_last_at = 2
                run_state.last_successful_artifact_at = 3
                run_state.artifacts.append({
                    "kind": "file",
                    "path": "GAL3_AD_FULL_REPORT.md",
                    "size_bytes": report.stat().st_size,
                })
                run_state.skill_workflow_contracts["healthsim-trialsim"] = {
                    "requires_merge": True,
                    "output_contract": {
                        "declared_final_artifact": "{TRIAL_NAME}_FULL_REPORT.md",
                        "expected_min_bytes": 10,
                        "expected_min_lines": 5,
                        "declared_section_count": 5,
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

                self.assertEqual(payload["verdict"], "pass")
                self.assertFalse(payload["needs_more_work"])
                self.assertIn("Earlier compacted-history placeholder", payload["reason"])


if __name__ == "__main__":
    unittest.main()
