import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
from agent_loop import HarnessRunState, _deterministic_verifier_payload


class VerifierFailurePayloadTests(unittest.TestCase):
    def test_readme_and_checklist_business_placeholders_are_not_universal_failures(self):
        """Backlog vocabulary and reusable templates are valid artifact data."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "roadmap")
                files = {
                    "README.md": (
                        "# Workflow vocabulary\n\n"
                        "`TODO` is the canonical name of the intake column.\n"
                    ),
                    "checklist.md": (
                        "# Reusable release template\n\n"
                        "- [ ] TODO: replace the example service name\n"
                        "- [ ] TBD by the team instantiating this template\n"
                    ),
                }
                for name, content in files.items():
                    (workspace / name).write_text(content, encoding="utf-8")
                run_state = HarnessRunState(user_id="u", session_id="roadmap")
                run_state.artifacts = [
                    {
                        "kind": "file",
                        "path": name,
                        "size_bytes": (workspace / name).stat().st_size,
                    }
                    for name in files
                ]

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                )

        self.assertEqual("pass", payload["verdict"], payload["findings"])
        self.assertFalse(payload["needs_more_work"])

    def test_declared_final_template_can_intentionally_contain_todo_tokens(self):
        """Declaring an artifact final does not turn its business data into placeholders."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "template")
                template = workspace / "issue_template.md"
                template.write_text(
                    "# Issue template\n\n- [ ] TODO: describe the incident\n",
                    encoding="utf-8",
                )
                output_contract = {
                    "declared_artifacts": ["issue_template.md"],
                    "declared_final_artifact": "issue_template.md",
                    "declared_file_count": 1,
                    "artifact_set_policy": {
                        "mode": "exact",
                        "artifacts": ["issue_template.md"],
                    },
                }
                run_state = HarnessRunState(
                    user_id="u",
                    session_id="template",
                    skill_workflow_activation="explicit_skill_request",
                )
                run_state.artifacts = [{
                    "kind": "file",
                    "path": "issue_template.md",
                    "size_bytes": template.stat().st_size,
                }]
                run_state.skill_workflow_contracts["issue-template"] = {
                    "output_contract": output_contract,
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                )

        self.assertEqual("pass", payload["verdict"], payload["findings"])
        self.assertTrue(payload["artifact_contract"]["valid"])

    def test_explicit_release_checklist_completion_policy_rejects_pending_rows(self):
        """A Skill can opt a real completion checklist into pending-marker checks."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "release")
                checklist = workspace / "release_checklist.md"
                checklist.write_text(
                    "| Check | Status |\n"
                    "|---|---|\n"
                    "| Production smoke test | TODO |\n",
                    encoding="utf-8",
                )
                output_contract = {
                    "declared_artifacts": ["release_checklist.md"],
                    "declared_final_artifact": "release_checklist.md",
                    "declared_file_count": 1,
                    "artifact_set_policy": {
                        "mode": "exact",
                        "artifacts": ["release_checklist.md"],
                    },
                }
                quality_contract = {
                    "checklist": {
                        "file": "release_checklist.md",
                        "format": "markdown",
                        "rows": 1,
                        "require_status": True,
                        "pending_markers": True,
                    },
                }
                run_state = HarnessRunState(
                    user_id="u",
                    session_id="release",
                    skill_workflow_activation="explicit_skill_request",
                )
                run_state.artifacts = [{
                    "kind": "file",
                    "path": "release_checklist.md",
                    "size_bytes": checklist.stat().st_size,
                }]
                run_state.skill_workflow_contracts["release-gate"] = {
                    "output_contract": output_contract,
                    "execution_contract": {
                        "output_contract": output_contract,
                        "quality_contract": quality_contract,
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                )

        self.assertEqual("fail", payload["verdict"])
        self.assertTrue(payload["needs_more_work"])
        self.assertFalse(payload["artifact_contract"]["valid"])
        self.assertTrue(any(
            "checklist_pending_marker" in finding
            or "checklist_status_not_complete" in finding
            for finding in payload["findings"]
        ), payload["findings"])

    def test_current_run_baseline_excludes_preexisting_workspace_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "baseline")
                (workspace / "input.json").write_text(
                    '{"kind": "input"}', encoding="utf-8"
                )
                output = workspace / "result.json"
                output.write_text('{"records": []}', encoding="utf-8")
                run_state = HarnessRunState(
                    user_id="u",
                    session_id="baseline",
                    skill_workflow_activation="explicit_skill_request",
                    workspace_baseline_paths={"input.json"},
                )
                run_state.artifacts.append({
                    "kind": "file",
                    "path": "result.json",
                    "size_bytes": output.stat().st_size,
                })
                run_state.skill_workflow_contracts["json-export"] = {
                    "output_contract": {
                        "declared_artifacts": ["result.json"],
                        "artifact_set_policy": {
                            "mode": "exact",
                            "artifacts": ["result.json"],
                        },
                    }
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                )

        self.assertEqual("pass", payload["verdict"], payload["findings"])
        self.assertFalse(payload["needs_more_work"])

    def test_non_markdown_full_output_uses_declared_artifact_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "html")
                output = workspace / "site" / "index.html"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    "<!doctype html><title>Plan</title><main>Complete.</main>",
                    encoding="utf-8",
                )
                run_state = HarnessRunState(
                    user_id="u",
                    session_id="html",
                    skill_workflow_activation="explicit_skill_request",
                )
                run_state.artifacts.append({
                    "kind": "file",
                    "path": "site/index.html",
                    "size_bytes": output.stat().st_size,
                })
                run_state.skill_workflow_contracts["html-package"] = {
                    "output_contract": {
                        "declared_artifacts": ["site/index.html"],
                        "declared_final_artifact": "site/index.html",
                        "declared_file_count": 1,
                        "merge_mandatory": False,
                        "artifact_set_policy": {
                            "mode": "exact",
                            "artifacts": ["site/index.html"],
                        },
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

        self.assertEqual("pass", payload["verdict"])
        self.assertFalse(payload["needs_more_work"])

    def test_non_markdown_final_does_not_bypass_markdown_module_quality(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "mixed")
                module = workspace / "01_notes.md"
                final = workspace / "site" / "index.html"
                final.parent.mkdir(parents=True, exist_ok=True)
                module.write_text("# Notes\nEvidence.\n", encoding="utf-8")
                final.write_text(
                    "<!doctype html><title>Decision</title><p>Complete.</p>",
                    encoding="utf-8",
                )
                output = {
                    "declared_modular_files": ["01_notes.md"],
                    "declared_final_artifact": "site/index.html",
                    "declared_file_count": 2,
                    "merge_mandatory": False,
                    "artifact_set_policy": {
                        "mode": "exact",
                        "artifacts": ["01_notes.md", "site/index.html"],
                    },
                }
                quality = {"required_module_markers": ["**Owner**"]}
                run_state = HarnessRunState(
                    user_id="u",
                    session_id="mixed",
                    skill_workflow_activation="explicit_skill_request",
                )
                run_state.artifacts = [
                    {"kind": "file", "path": path, "size_bytes": target.stat().st_size}
                    for path, target in (
                        ("01_notes.md", module),
                        ("site/index.html", final),
                    )
                ]
                run_state.skill_workflow_contracts["mixed-package"] = {
                    "output_contract": output,
                    "execution_contract": {
                        "output_contract": output,
                        "quality_contract": quality,
                    },
                }

                failed = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )
                module.write_text(
                    "# Notes\n**Owner**: analyst\nEvidence.\n",
                    encoding="utf-8",
                )
                passed = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

        self.assertEqual("fail", failed["verdict"])
        self.assertTrue(any(
            "missing skill-declared markers" in finding
            for finding in failed["findings"]
        ))
        self.assertEqual("pass", passed["verdict"], passed["findings"])

    def test_multiple_skills_are_verified_by_identity_without_contract_guessing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "multi")
                (workspace / "data.json").write_text(
                    '{"status":"ok"}',
                    encoding="utf-8",
                )
                (workspace / "notes.md").write_text(
                    "# Notes\n\nComplete.\n",
                    encoding="utf-8",
                )
                run_state = HarnessRunState(
                    user_id="u",
                    session_id="multi",
                    skill_workflow_activation="explicit_skill_request",
                )
                for path in ("data.json", "notes.md"):
                    run_state.artifacts.append({
                        "kind": "file",
                        "path": path,
                        "size_bytes": (workspace / path).stat().st_size,
                    })
                run_state.skill_workflow_contracts["json-skill"] = {
                    "output_contract": {
                        "declared_artifacts": ["data.json"],
                        "declared_file_count": 1,
                        "artifact_formats": {"data.json": "json"},
                        "artifact_set_policy": {
                            "mode": "exact",
                            "artifacts": ["data.json"],
                        },
                    },
                }
                run_state.skill_workflow_contracts["markdown-skill"] = {
                    "output_contract": {
                        "declared_artifacts": ["notes.md"],
                        "declared_file_count": 1,
                        "artifact_set_policy": {
                            "mode": "exact",
                            "artifacts": ["notes.md"],
                        },
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

                run_state.skill_workflow_contracts["markdown-skill"][
                    "output_contract"
                ]["expected_min_bytes"] = 10_000
                short_payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

        self.assertEqual("pass", payload["verdict"], payload["findings"])
        self.assertTrue(payload["artifact_contract"]["valid"])
        self.assertEqual(
            {"json-skill", "markdown-skill"},
            set(payload["artifact_contract"]["by_skill"]),
        )
        self.assertEqual("fail", short_payload["verdict"])
        self.assertTrue(any(
            "Skill 'markdown-skill'" in finding
            and "skill's own declared completion contract" in finding
            for finding in short_payload["findings"]
        ), short_payload["findings"])
        self.assertFalse(
            short_payload["artifact_contract"]["by_skill"]["markdown-skill"]["valid"]
        )

    def test_multiple_skills_cannot_implicitly_share_one_artifact_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "shared")
                shared = workspace / "shared.md"
                shared.write_text("# Shared\nSubstantive result.\n", encoding="utf-8")
                run_state = HarnessRunState(
                    user_id="u",
                    session_id="shared",
                    skill_workflow_activation="explicit_skill_request",
                )
                run_state.artifacts = [{
                    "kind": "file",
                    "path": "shared.md",
                    "size_bytes": shared.stat().st_size,
                }]
                for skill_name in ("skill-a", "skill-b"):
                    run_state.skill_workflow_contracts[skill_name] = {
                        "output_contract": {
                            "declared_artifacts": ["shared.md"],
                            "artifact_set_policy": {
                                "mode": "exact",
                                "artifacts": ["shared.md"],
                            },
                        },
                    }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

        self.assertEqual("fail", payload["verdict"])
        self.assertFalse(payload["artifact_contract"]["valid"])
        self.assertTrue(payload["artifact_contract"]["ownership_conflicts"])
        self.assertTrue(any(
            "Artifact ownership conflict" in finding
            for finding in payload["findings"]
        ))

    def test_simple_chat_tool_error_is_not_promoted_to_artifact_workflow(self):
        run_state = HarnessRunState(user_id="u", session_id="s")
        run_state.tool_error_count = 1
        run_state.last_tool_error_at = 1

        payload = _deterministic_verifier_payload(
            run_state,
            requested_artifact=False,
            complex_report=False,
        )

        self.assertIsNone(payload)

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
