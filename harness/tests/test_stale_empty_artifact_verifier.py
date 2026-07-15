import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
from agent_loop import HarnessRunState, _deterministic_verifier_payload


class StaleEmptyArtifactVerifierTests(unittest.TestCase):
    def test_later_non_empty_workspace_write_overrides_empty_artifact_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                workspace.mkdir(parents=True, exist_ok=True)
                report = workspace / "GAL3_AD_FULL_REPORT.md"
                report.write_text("", encoding="utf-8")
                report.write_text(
                    "# Report\n## A\ncontent\n## B\ncontent\n## C\ncontent\n",
                    encoding="utf-8",
                )
                run_state = HarnessRunState(user_id="u", session_id="s")
                run_state.artifacts.extend([
                    {
                        "kind": "file",
                        "path": "GAL3_AD_FULL_REPORT.md",
                        "size_bytes": 0,
                    },
                    {
                        "kind": "file",
                        "path": "GAL3_AD_FULL_REPORT.md",
                        "size_bytes": report.stat().st_size,
                    },
                ])
                run_state.skill_workflow_contracts["healthsim-trialsim"] = {
                    "requires_merge": True,
                    "output_contract": {
                        "declared_final_artifact": "{TRIAL_NAME}_FULL_REPORT.md",
                        "expected_min_bytes": 10,
                        "expected_min_lines": 5,
                        "declared_section_count": 3,
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

                self.assertEqual(payload["verdict"], "pass")
                self.assertFalse(payload["needs_more_work"])
                self.assertNotIn("Artifact is empty", payload["reason"])


if __name__ == "__main__":
    unittest.main()
