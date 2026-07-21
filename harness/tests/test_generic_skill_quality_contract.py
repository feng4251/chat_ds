import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
from agent_loop import HarnessRunState, _markdown_quality_findings


def _contract(*, max_total_lines: int = 30) -> dict:
    output = {
        "declared_modular_files": ["01_summary.md", "02_evidence.md"],
        "declared_ancillary_files": ["README.md", "_checklist.md"],
        "declared_final_artifact": "{NAME}_FULL_REPORT.md",
        "declared_file_count": 5,
        "declared_section_count": 2,
        "expected_min_bytes": 20,
        "expected_min_lines": 4,
        "artifact_index": {
            "file": "README.md",
            "coverage_mode": "declared_outputs",
        },
    }
    quality = {
        "required_module_markers": ["**Owner**", "**Source**"],
        "section_file_mapping": [
            {"file": "01_summary.md", "section_ids": ["summary"]},
            {"file": "02_evidence.md", "section_ids": ["evidence"]},
        ],
        "constraints": {
            "max_lines_per_file": 20,
            "max_total_lines": max_total_lines,
            "declared_checklist_rows": 2,
        },
    }
    return {
        "output_contract": output,
        "execution_contract": {
            "quality_contract": quality,
            "output_contract": output,
        },
    }


def _files() -> dict[str, str]:
    first = (
        "# Summary\n"
        "**Owner**: worker-a\n"
        "**Source**: REF-001\n"
        "## Decision\n"
        "content\n"
    )
    second = (
        "# Evidence\n"
        "**Owner**: worker-b\n"
        "**Source**: REF-002\n"
        "## Findings\n"
        "content\n"
    )
    return {
        "01_summary.md": first,
        "02_evidence.md": second,
        "README.md": (
            "# Index\n"
            "- 01_summary.md\n"
            "- 02_evidence.md\n"
            "- _checklist.md\n"
            "- TEST_FULL_REPORT.md\n"
        ),
        "_checklist.md": (
            "| # | Section | File | Status |\n"
            "|---|---|---|---|\n"
            "| 1 | summary | 01_summary.md | ✓ |\n"
            "| 2 | evidence | 02_evidence.md | ✓ |\n"
        ),
        "TEST_FULL_REPORT.md": first + second,
    }


class GenericSkillQualityContractTests(unittest.TestCase):
    def _findings(
        self,
        files: dict[str, str],
        *,
        contract: dict | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                for name, content in files.items():
                    (workspace / name).write_text(content, encoding="utf-8")
                state = HarnessRunState(user_id="u", session_id="s")
                state.skill_workflow_contracts["generic"] = contract or _contract()
                state.artifacts = [
                    {
                        "path": name,
                        "size_bytes": len(content.encode("utf-8")),
                    }
                    for name, content in files.items()
                ]
                return _markdown_quality_findings(
                    state,
                    ["TEST_FULL_REPORT.md"],
                    complex_report=True,
                )

    def test_explicit_structural_contract_passes(self):
        self.assertEqual([], self._findings(_files()))

    def test_checklist_row_shortfall_is_rejected(self):
        files = _files()
        files["_checklist.md"] = (
            "| # | Section | File | Status |\n"
            "|---|---|---|---|\n"
            "| 1 | summary | 01_summary.md | ✓ |\n"
        )
        findings = self._findings(files)
        self.assertTrue(any("Checklist" in item and "data rows" in item for item in findings))

    def test_total_module_line_limit_is_rejected_without_double_counting_merge(self):
        findings = self._findings(_files(), contract=_contract(max_total_lines=9))
        self.assertTrue(any("modular content lines" in item for item in findings))

    def test_empty_or_template_marker_value_is_rejected(self):
        files = _files()
        files["01_summary.md"] = (
            "# Summary\n"
            "**Owner**:\n"
            "**Source**: [source]\n"
            "## Decision\n"
            "content\n"
        )
        files["TEST_FULL_REPORT.md"] = (
            files["01_summary.md"] + files["02_evidence.md"]
        )
        findings = self._findings(files)
        self.assertTrue(any("template-placeholder" in item for item in findings))

    def test_marker_in_code_example_does_not_satisfy_contract(self):
        files = _files()
        files["01_summary.md"] = (
            "# Summary\n"
            "```markdown\n"
            "**Owner**: worker-a\n"
            "**Source**: REF-001\n"
            "```\n"
            "## Decision\n"
            "content\n"
        )
        files["TEST_FULL_REPORT.md"] = (
            files["01_summary.md"] + files["02_evidence.md"]
        )
        findings = self._findings(files)
        self.assertTrue(any("missing skill-declared markers" in item for item in findings))

    def test_marker_label_requires_a_token_boundary(self):
        files = _files()
        files["01_summary.md"] = (
            "# Summary\n"
            "Ownership is unknown.\n"
            "**Source**: REF-001\n"
            "## Decision\n"
            "content\n"
        )
        files["TEST_FULL_REPORT.md"] = (
            files["01_summary.md"] + files["02_evidence.md"]
        )
        findings = self._findings(files)
        self.assertTrue(any("missing skill-declared markers" in item for item in findings))

    def test_declared_readme_index_must_cover_declared_outputs(self):
        files = _files()
        files["README.md"] = "# Index\n- 01_summary.md\n"
        findings = self._findings(files)
        self.assertTrue(any("README index" in item for item in findings))

    def test_structured_section_mapping_requires_assigned_heading_groups(self):
        files = _files()
        contract = _contract()
        contract["execution_contract"]["quality_contract"]["section_file_mapping"][1][
            "section_ids"
        ] = ["evidence", "provenance"]
        findings = self._findings(files, contract=contract)
        self.assertTrue(any("section-to-file mapping" in item for item in findings))

    def test_format_only_heading_groups_remain_authoritative_for_nonresearch_module(self):
        files = _files()
        contract = _contract()
        contract["execution_contract"]["quality_contract"]["section_file_mapping"][1] = {
            "file": "02_evidence.md",
            "section_ids": [],
            "raw_sections": "Inventory Totals + Exception Bins",
            "unresolved_sections": ["Inventory Totals", "Exception Bins"],
            "enforce_heading_count": True,
            "required_heading_groups": 2,
        }

        findings = self._findings(files, contract=contract)

        self.assertTrue(any(
            "02_evidence.md" in item and "section groups" in item
            for item in findings
        ))

    def test_checklist_section_to_file_swap_is_rejected(self):
        files = _files()
        files["_checklist.md"] = (
            "| # | Section | File | Status |\n"
            "|---|---|---|---|\n"
            "| 1 | summary | 02_evidence.md | ✓ |\n"
            "| 2 | evidence | 01_summary.md | ✓ |\n"
        )
        findings = self._findings(files)
        self.assertTrue(any("wrong file" in item for item in findings))

    def test_additional_markdown_is_rejected_only_for_explicit_exact_policy(self):
        files = _files()
        files["notes.md"] = "# Extra\n"
        self.assertEqual([], self._findings(files))

        contract = _contract()
        contract["output_contract"]["artifact_set_policy"] = {
            "mode": "exact",
            "artifacts": [
                "01_summary.md",
                "02_evidence.md",
                "README.md",
                "_checklist.md",
                "{NAME}_FULL_REPORT.md",
            ],
        }
        contract["execution_contract"]["output_contract"] = contract["output_contract"]
        findings = self._findings(files, contract=contract)
        self.assertTrue(any("exact artifact set" in item for item in findings))

    def test_boundary_check_uses_explicit_merge_input_order(self):
        files = _files()
        contract = _contract()
        output = contract["output_contract"]
        output["merge_input_order"] = ["02_evidence.md", "01_summary.md"]
        output["verify_first_last_match"] = True
        files["TEST_FULL_REPORT.md"] = (
            files["02_evidence.md"] + files["01_summary.md"]
        )
        self.assertEqual([], self._findings(files, contract=contract))

        files["TEST_FULL_REPORT.md"] = (
            files["01_summary.md"] + files["02_evidence.md"]
        )
        findings = self._findings(files, contract=contract)
        self.assertTrue(any(
            "first non-empty line" in item or "last non-empty line" in item
            for item in findings
        ))


if __name__ == "__main__":
    unittest.main()
