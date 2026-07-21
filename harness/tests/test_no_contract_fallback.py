import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
from agent_loop import (
    HarnessRunState,
    _is_likely_terminal_report_artifact,
    _markdown_quality_findings,
)


def _dense_evidence_report(title: str) -> str:
    sections = [f"# {title}\n"]
    for section in range(20):
        sections.extend([
            f"## {section + 1}. Evidence sources and results\n",
            f"### {section + 1}.1 Method and traceability\n",
            f"### {section + 1}.2 References and appendix\n",
            "| ID | Evidence source | Trace result | Citation |\n",
            "|---|---|---|---|\n",
        ])
        for row in range(80):
            sections.append(
                f"| {section + 1}-{row + 1} | Authoritative evidence source "
                f"{row + 1} with supporting context | Traceable result and "
                "reviewed output | "
                f"[Citation {section + 1}-{row + 1}]"
                f"(https://example.test/evidence/{section + 1}/{row + 1}) |\n"
            )
    sections.extend([
        "## Appendix: evidence traceability matrix\n",
        "### Appendix A: source register\n",
        "### Appendix B: result verification\n",
    ])
    return "".join(sections)


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

    def test_table_dense_multilingual_final_satisfies_fallback_without_code_blocks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "dense")
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "draft_report.md").write_text(
                    "# Draft\n" + "unfinished prose\n" * 2_000,
                    encoding="utf-8",
                )
                (workspace / "_continuation.md").write_text(
                    "# Continuation\n" + "intermediate\n" * 1_000,
                    encoding="utf-8",
                )

                sections = ["# 综合分析报告\n"]
                for section in range(20):
                    sections.extend([
                        f"## {section + 1}. 证据、来源与结果\n",
                        f"### {section + 1}.1 方法与追溯\n",
                        f"### {section + 1}.2 引用与文献\n",
                        "| 编号 | 证据来源 | 追溯结果 | 参考链接 |\n",
                        "|---|---|---|---|\n",
                    ])
                    for row in range(80):
                        sections.append(
                            f"| {section + 1}-{row + 1} | 权威数据源与循证依据 "
                            f"{row + 1} | 已追溯并形成交付物输出结果 | "
                            f"[引用 {section + 1}-{row + 1}]"
                            f"(https://example.test/evidence/{section + 1}/{row + 1}) |\n"
                        )
                sections.extend([
                    "## 附录：证据溯源矩阵\n",
                    "### 附录 A：来源清单\n",
                    "### 附录 B：结果复核\n",
                ])
                final_content = "".join(sections)
                self.assertGreater(len(final_content.encode("utf-8")), 210_000)
                self.assertNotIn("```", final_content)
                (workspace / "analysis_final.md").write_text(
                    final_content,
                    encoding="utf-8",
                )

                run_state = HarnessRunState(user_id="u", session_id="dense")
                run_state.successful_search_count = 3
                run_state.successful_code_execution_count = 1
                findings = _markdown_quality_findings(
                    run_state,
                    [
                        "draft_report.md",
                        "_continuation.md",
                        "analysis_final.md",
                    ],
                    complex_report=True,
                )

                self.assertEqual([], findings)

    def test_all_thin_drafts_report_only_best_terminal_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "thin")
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "_expansion.md").write_text(
                    "# Scratch\nshort\n",
                    encoding="utf-8",
                )
                (workspace / "draft_report.md").write_text(
                    "# Draft\n" + "draft\n" * 100,
                    encoding="utf-8",
                )
                (workspace / "analysis_final.md").write_text(
                    "# Final\n## Result\nbrief\n",
                    encoding="utf-8",
                )

                run_state = HarnessRunState(user_id="u", session_id="thin")
                run_state.successful_search_count = 1
                findings = _markdown_quality_findings(
                    run_state,
                    ["_expansion.md", "draft_report.md", "analysis_final.md"],
                    complex_report=True,
                )

                self.assertEqual(1, len(findings))
                self.assertIn("analysis_final.md", findings[0])
                self.assertNotIn("_expansion.md", findings[0])
                self.assertNotIn("draft_report.md", findings[0])

    def test_dense_ancillary_report_cannot_mask_thin_explicit_final(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "masked-final")
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "evidence_report.md").write_text(
                    _dense_evidence_report("Evidence Report"),
                    encoding="utf-8",
                )
                (workspace / "deliverable_final.md").write_text(
                    "# Final\n## Result\nbrief\n",
                    encoding="utf-8",
                )

                run_state = HarnessRunState(
                    user_id="u", session_id="masked-final"
                )
                run_state.successful_search_count = 1
                findings = _markdown_quality_findings(
                    run_state,
                    ["evidence_report.md", "deliverable_final.md"],
                    complex_report=True,
                )

                self.assertEqual(1, len(findings))
                self.assertIn("deliverable_final.md", findings[0])
                self.assertNotIn("evidence_report.md", findings[0])

    def test_thin_ancillary_report_does_not_block_dense_explicit_final(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace(
                    "u", "valid-final-with-index"
                )
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "evidence_report.md").write_text(
                    "# Evidence index\nSee the final deliverable.\n",
                    encoding="utf-8",
                )
                (workspace / "deliverable_final.md").write_text(
                    _dense_evidence_report("Final Deliverable"),
                    encoding="utf-8",
                )

                run_state = HarnessRunState(
                    user_id="u", session_id="valid-final-with-index"
                )
                run_state.successful_search_count = 1
                self.assertEqual(
                    [],
                    _markdown_quality_findings(
                        run_state,
                        ["evidence_report.md", "deliverable_final.md"],
                        complex_report=True,
                    ),
                )

        self.assertFalse(
            _is_likely_terminal_report_artifact("evidence_report.md")
        )
        self.assertFalse(
            _is_likely_terminal_report_artifact("incomplete_report.md")
        )
        self.assertFalse(
            _is_likely_terminal_report_artifact("finality_notes.md")
        )
        self.assertFalse(
            _is_likely_terminal_report_artifact("fulfillment.md")
        )

    def test_dense_strong_final_supersedes_thin_complete_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace(
                    "u", "strong-final-over-complete"
                )
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "development_plan_complete.md").write_text(
                    "# Complete checkpoint\n## Result\nbrief\n",
                    encoding="utf-8",
                )
                (workspace / "development_plan_final.md").write_text(
                    _dense_evidence_report("Final Development Plan"),
                    encoding="utf-8",
                )

                run_state = HarnessRunState(
                    user_id="u", session_id="strong-final-over-complete"
                )
                run_state.successful_search_count = 1
                self.assertEqual(
                    [],
                    _markdown_quality_findings(
                        run_state,
                        [
                            "development_plan_complete.md",
                            "development_plan_final.md",
                        ],
                        complex_report=True,
                    ),
                )

    def test_multiple_strong_finals_are_all_independently_checked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace(
                    "u", "multiple-strong-finals"
                )
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "clinical_final.md").write_text(
                    _dense_evidence_report("Clinical Final"),
                    encoding="utf-8",
                )
                (workspace / "regulatory_final.md").write_text(
                    "# Regulatory Final\n## Result\nbrief\n",
                    encoding="utf-8",
                )

                run_state = HarnessRunState(
                    user_id="u", session_id="multiple-strong-finals"
                )
                run_state.successful_search_count = 1
                findings = _markdown_quality_findings(
                    run_state,
                    ["clinical_final.md", "regulatory_final.md"],
                    complex_report=True,
                )

                self.assertEqual(1, len(findings))
                self.assertIn("regulatory_final.md", findings[0])
                self.assertNotIn("clinical_final.md", findings[0])

    def test_chinese_final_report_is_terminal_and_cannot_be_masked(self):
        self.assertTrue(
            _is_likely_terminal_report_artifact("项目最终报告.md")
        )
        self.assertTrue(
            _is_likely_terminal_report_artifact("調査最終報告書.md")
        )
        self.assertTrue(
            _is_likely_terminal_report_artifact("분석_최종보고서.md")
        )
        self.assertFalse(
            _is_likely_terminal_report_artifact("项目初稿报告.md")
        )
        self.assertFalse(
            _is_likely_terminal_report_artifact("調査報告書_作業中.md")
        )
        self.assertFalse(
            _is_likely_terminal_report_artifact("분석보고서_초안.md")
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "cjk-final")
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "evidence_bundle.md").write_text(
                    _dense_evidence_report("Evidence Bundle"),
                    encoding="utf-8",
                )
                (workspace / "项目最终报告.md").write_text(
                    "# 最终报告\n## 结果\n内容不足。\n",
                    encoding="utf-8",
                )

                run_state = HarnessRunState(user_id="u", session_id="cjk-final")
                run_state.successful_search_count = 1
                findings = _markdown_quality_findings(
                    run_state,
                    ["evidence_bundle.md", "项目最终报告.md"],
                    complex_report=True,
                )

                self.assertEqual(1, len(findings))
                self.assertIn("项目最终报告.md", findings[0])
                self.assertNotIn("evidence_bundle.md", findings[0])


if __name__ == "__main__":
    unittest.main()
