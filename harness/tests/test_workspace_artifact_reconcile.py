import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
from agent_loop import (
    HarnessRunState,
    _contract_artifact_matchers,
    _declared_workspace_file,
    _has_merged_artifact_for_contract,
    _markdown_quality_findings,
    _reconcile_workspace_artifacts,
)


class WorkspaceArtifactReconcileTests(unittest.TestCase):
    def test_structured_final_declaration_excludes_legacy_report_patterns(self):
        contract = {
            "artifact_patterns": ["report.md", "notes.md"],
            "output_contract": {
                "declared_final_artifact": "{NAME}_FULL_REPORT.md",
                "declared_modular_files": ["01_intro.md"],
            },
        }
        final_patterns, modular_patterns = _contract_artifact_matchers(contract)
        self.assertEqual(["*_full_report.md"], final_patterns)
        self.assertEqual(["01_intro.md"], modular_patterns)

    def test_reconcile_does_not_credit_files_older_than_the_current_run(self):
        contract = {
            "output_contract": {
                "declared_final_artifact": "{NAME}_FULL_REPORT.md",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                final = workspace / "OLD_FULL_REPORT.md"
                final.write_text("# stale\n", encoding="utf-8")
                state = HarnessRunState(
                    user_id="u",
                    session_id="s",
                    started_at_epoch=final.stat().st_mtime + 1,
                )
                self.assertEqual(
                    [],
                    _reconcile_workspace_artifacts(state, [contract]),
                )

    def test_reconciled_final_needs_native_merge_receipt_when_merge_is_mandatory(self):
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
                self.assertFalse(_has_merged_artifact_for_contract(run_state, contract))
                run_state.artifacts.append({
                    "path": "GAL3_AD_FULL_REPORT.md",
                    "source_tool": "merge_files",
                    "is_merged": True,
                    "input_files": ["01_intro.md", "02_methods.md"],
                    "size_bytes": final.stat().st_size,
                })
                self.assertTrue(_has_merged_artifact_for_contract(run_state, contract))
                self.assertEqual(
                    [],
                    _markdown_quality_findings(
                        run_state,
                        ["GAL3_AD_FULL_REPORT.md"],
                        complex_report=True,
                    ),
                )

    def test_root_declaration_does_not_reconcile_nested_same_name(self):
        contract = {
            "output_contract": {
                "declared_final_artifact": "{NAME}_FULL_REPORT.md",
                "declared_modular_files": ["reports/*.md"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "nested")
                nested = workspace / "nested"
                deep_reports = workspace / "reports" / "deep"
                nested.mkdir(parents=True)
                deep_reports.mkdir(parents=True)
                (nested / "ALPHA_FULL_REPORT.md").write_text(
                    "# Wrong depth\n", encoding="utf-8"
                )
                (deep_reports / "module.md").write_text(
                    "# Wrong depth\n", encoding="utf-8"
                )
                state = HarnessRunState(user_id="u", session_id="nested")
                self.assertEqual([], _reconcile_workspace_artifacts(state, [contract]))

    def test_ambiguous_placeholder_is_not_reconciled_or_selected(self):
        contract = {
            "output_contract": {
                "declared_final_artifact": "{NAME}_FULL_REPORT.md",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "ambiguous")
                workspace.mkdir(parents=True, exist_ok=True)
                for name in ("A_FULL_REPORT.md", "B_FULL_REPORT.md"):
                    (workspace / name).write_text("# Report\n", encoding="utf-8")
                state = HarnessRunState(user_id="u", session_id="ambiguous")
                self.assertEqual([], _reconcile_workspace_artifacts(state, [contract]))
                self.assertIsNone(
                    _declared_workspace_file(workspace, "{NAME}_FULL_REPORT.md")
                )

    def test_structured_output_lookup_discards_a_scan_over_limit(self):
        contract = {
            "output_contract": {
                "declared_modular_files": ["result.json"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "bounded")
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "result.json").write_text(
                    '{"status": "complete"}',
                    encoding="utf-8",
                )
                (workspace / "input.csv").write_text(
                    "name,value\nalpha,1\n",
                    encoding="utf-8",
                )
                state = HarnessRunState(user_id="u", session_id="bounded")
                with patch("agent_loop._MAX_RECONCILE_FILES", 1):
                    self.assertIsNone(
                        _declared_workspace_file(workspace, "result.json")
                    )
                    self.assertEqual(
                        [],
                        _reconcile_workspace_artifacts(state, [contract]),
                    )
                self.assertEqual([], state.artifacts)


if __name__ == "__main__":
    unittest.main()
