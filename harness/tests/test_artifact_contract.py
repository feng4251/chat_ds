import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from artifact_contract import verify_artifact_contract


class ArtifactContractVerifierTests(unittest.TestCase):
    def _markdown_contract(self) -> dict:
        return {
            "declared_modular_files": ["01_summary.md", "02_evidence.md"],
            "declared_ancillary_files": ["README.md", "_checklist.md"],
            "declared_final_artifact": "{PROJECT}_FULL.md",
            "declared_file_count": 5,
            "declared_section_count": 2,
            "merge_mandatory": True,
            "quality_contract": {
                "checklist": {
                    "rows": 3,
                    "row_mode": "exact",
                    "require_status": True,
                    "require_merge_receipt": True,
                    "pending_markers": True,
                },
                "pending_markers": True,
                "detect_padding": True,
            },
            "artifact_index": {
                "file": "README.md",
                "coverage_mode": "declared_outputs",
            },
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": [
                    "01_summary.md",
                    "02_evidence.md",
                    "README.md",
                    "_checklist.md",
                    "{PROJECT}_FULL.md",
                ],
            },
        }

    def _write_markdown_package(self, root: Path) -> dict[str, str]:
        first = (
            "# Summary\n"
            "**Owner**: planning-worker\n"
            "\n"
            "The decision summary records the selected option and its supporting rationale.\n"
        )
        second = (
            "# Evidence\n"
            "**Owner**: evidence-worker\n"
            "\n"
            "The evidence module records provenance, limitations, and the remaining uncertainty.\n"
        )
        files = {
            "01_summary.md": first,
            "02_evidence.md": second,
            "README.md": (
                "# Package index\n\n"
                "- [Summary](01_summary.md)\n"
                "- [Evidence](02_evidence.md)\n"
                "- [Audit](_checklist.md)\n"
                "- [Merged report](DEMO_FULL.md)\n"
            ),
            "_checklist.md": (
                "# Completion audit\n\n"
                "| # | Section | File | Status |\n"
                "|---|---|---|---|\n"
                "| 1 | Summary | 01_summary.md | ✓ |\n"
                "| 2 | Evidence | 02_evidence.md | PASS |\n"
                "| 3 | Auto-Merge Full Report | DEMO_FULL.md | completed |\n"
            ),
            "DEMO_FULL.md": first + second,
        }
        for name, content in files.items():
            (root / name).write_text(content, encoding="utf-8")
        return files

    def _codes(self, result: dict) -> set[str]:
        return {str(item.get("code")) for item in result.get("findings") or []}

    def test_semantic_final_is_not_forced_to_equal_modules_without_merge_contract(self):
        contract = {
            "declared_modular_files": ["evidence.md", "analysis.md"],
            "declared_final_artifact": "decision.html",
            "merge_mandatory": False,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "evidence.md").write_text("# Evidence\n\nFact A.\n", encoding="utf-8")
            (root / "analysis.md").write_text("# Analysis\n\nTrade-off B.\n", encoding="utf-8")
            (root / "decision.html").write_text(
                "<!doctype html><title>Decision</title><p>Select option C.</p>",
                encoding="utf-8",
            )
            result = verify_artifact_contract(root, contract)

        self.assertTrue(result["valid"], result["findings"])
        self.assertNotIn("merged_content_mismatch", self._codes(result))
        self.assertNotIn("merge", result["metrics"])

    def test_legacy_merge_command_without_flag_requires_exact_bytes(self):
        contract = {
            "declared_modular_files": ["one.txt", "two.txt"],
            "declared_final_artifact": "FINAL.txt",
            "merge_command": "cat one.txt two.txt > FINAL.txt",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            (root / "two.txt").write_text("two\n", encoding="utf-8")
            (root / "FINAL.txt").write_text("semantic summary\n", encoding="utf-8")
            result = verify_artifact_contract(root, contract)

        self.assertFalse(result["valid"])
        self.assertIn("merged_content_mismatch", self._codes(result))
        self.assertTrue(result["metrics"]["merge"]["required"])

    def test_explicit_merge_input_order_is_authoritative(self):
        contract = {
            "declared_modular_files": ["a.txt", "b.txt"],
            "declared_final_artifact": "FINAL.txt",
            "merge_mandatory": True,
            "merge_input_order": ["b.txt", "a.txt"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_text("A", encoding="utf-8")
            (root / "b.txt").write_text("B", encoding="utf-8")
            (root / "FINAL.txt").write_text("BA", encoding="utf-8")
            correct = verify_artifact_contract(root, contract)
            (root / "FINAL.txt").write_text("AB", encoding="utf-8")
            wrong = verify_artifact_contract(root, contract)

        self.assertTrue(correct["valid"], correct["findings"])
        self.assertEqual(
            ["b.txt", "a.txt"],
            correct["metrics"]["merge"]["input_files"],
        )
        self.assertFalse(wrong["valid"])
        self.assertIn("merged_content_mismatch", self._codes(wrong))

    def test_non_markdown_final_obeys_declared_completion_bounds(self):
        contract = {
            "declared_final_artifact": "site/index.html",
            "expected_min_bytes": 10_000,
            "expected_min_lines": 500,
            "expected_max_lines": 1_000,
            "merge_mandatory": False,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "site" / "index.html"
            target.parent.mkdir(parents=True)
            target.write_text("<!doctype html>\n<title>Brief</title>\n", encoding="utf-8")
            result = verify_artifact_contract(root, contract)

        self.assertFalse(result["valid"])
        self.assertIn("artifact_min_bytes_not_met", self._codes(result))
        self.assertIn("artifact_min_lines_not_met", self._codes(result))
        self.assertEqual("site/index.html", result["metrics"]["completion"]["artifact"])

        contract["expected_min_bytes"] = 1
        contract["expected_min_lines"] = 1
        contract["expected_max_lines"] = 1
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "site" / "index.html"
            target.parent.mkdir(parents=True)
            target.write_text("one\ntwo\n", encoding="utf-8")
            too_many_lines = verify_artifact_contract(root, contract)
        self.assertIn("artifact_max_lines_exceeded", self._codes(too_many_lines))

    def test_markdown_package_passes_with_exact_current_merge_and_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_markdown_package(root)
            result = verify_artifact_contract(root, self._markdown_contract())

        self.assertTrue(result["valid"], result["findings"])
        self.assertEqual([], result["findings"])
        self.assertTrue(result["metrics"]["merge"]["byte_equal"])
        self.assertEqual(
            result["metrics"]["merge"]["expected_sha256"],
            result["metrics"]["merge"]["actual_sha256"],
        )
        self.assertTrue(result["metrics"]["checklist"]["merge_receipt"])

    def test_markdown_contract_mutations_fail_with_stable_codes(self):
        mutations = {}

        def missing_module(root: Path, files: dict[str, str]) -> None:
            (root / "02_evidence.md").unlink()

        mutations["missing module"] = (
            missing_module,
            "declared_artifact_missing",
        )

        def extra_artifact(root: Path, files: dict[str, str]) -> None:
            (root / "notes.md").write_text("# Undeclared\n", encoding="utf-8")

        mutations["extra exact-set artifact"] = (
            extra_artifact,
            "unexpected_artifact",
        )

        def stale_merge(root: Path, files: dict[str, str]) -> None:
            (root / "01_summary.md").write_text(
                files["01_summary.md"] + "A later correction changed this module.\n",
                encoding="utf-8",
            )

        mutations["module changed after merge"] = (
            stale_merge,
            "merged_content_mismatch",
        )

        def dead_readme_link(root: Path, files: dict[str, str]) -> None:
            (root / "README.md").write_text(
                files["README.md"] + "- [Missing](absent.md)\n",
                encoding="utf-8",
            )

        mutations["dead README link"] = (
            dead_readme_link,
            "readme_link_target_missing",
        )

        def false_checklist_status(root: Path, files: dict[str, str]) -> None:
            (root / "_checklist.md").write_text(
                files["_checklist.md"].replace(
                    "| 2 | Evidence | 02_evidence.md | PASS |",
                    "| 2 | Evidence | 02_evidence.md | ✓ TODO |",
                ),
                encoding="utf-8",
            )

        mutations["TODO cannot masquerade as pass"] = (
            false_checklist_status,
            "checklist_status_invalid",
        )

        def missing_merge_receipt(root: Path, files: dict[str, str]) -> None:
            content = files["_checklist.md"].replace(
                "| 3 | Auto-Merge Full Report | DEMO_FULL.md | completed |\n",
                "",
            )
            (root / "_checklist.md").write_text(content, encoding="utf-8")

        mutations["missing merge receipt"] = (
            missing_merge_receipt,
            "checklist_merge_receipt_missing",
        )

        def blank_padding(root: Path, files: dict[str, str]) -> None:
            first = files["01_summary.md"] + ("\n" * 60) + "End of substantive module.\n"
            (root / "01_summary.md").write_text(first, encoding="utf-8")
            (root / "DEMO_FULL.md").write_text(
                first + files["02_evidence.md"],
                encoding="utf-8",
            )

        mutations["blank padding"] = (
            blank_padding,
            "excessive_blank_line_padding",
        )

        def repeated_padding(root: Path, files: dict[str, str]) -> None:
            paragraph = (
                "This repeated paragraph is deliberately long enough to be substantive, "
                "but repeating it adds no new evidence, decision, provenance, or analysis."
            )
            first = "# Summary\n\n" + "\n\n".join([paragraph] * 6) + "\n"
            (root / "01_summary.md").write_text(first, encoding="utf-8")
            (root / "DEMO_FULL.md").write_text(
                first + files["02_evidence.md"],
                encoding="utf-8",
            )

        mutations["repeated paragraph padding"] = (
            repeated_padding,
            "repeated_content_padding",
        )

        def empty_module(root: Path, files: dict[str, str]) -> None:
            (root / "01_summary.md").write_text("\n\n", encoding="utf-8")
            (root / "DEMO_FULL.md").write_text(
                "\n\n" + files["02_evidence.md"],
                encoding="utf-8",
            )

        mutations["empty module"] = (
            empty_module,
            "empty_artifact",
        )

        for label, (mutation, expected_code) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                files = self._write_markdown_package(root)
                mutation(root, files)
                result = verify_artifact_contract(root, self._markdown_contract())
                self.assertFalse(result["valid"])
                self.assertIn(expected_code, self._codes(result), result["findings"])

    def test_nonmerge_json_contract_is_supported_without_report_assumptions(self):
        contract = {
            "declared_modular_files": ["catalog.json"],
            "declared_ancillary_files": ["README.md", "_checklist.md"],
            "declared_file_count": 3,
            "declared_section_count": 1,
            "artifact_index": {
                "file": "README.md",
                "coverage_mode": "declared_outputs",
            },
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": ["catalog.json", "README.md", "_checklist.md"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "catalog.json").write_text(
                json.dumps({"objects": [{"accession": "M-001", "status": "verified"}]}),
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "# Inventory\n\n[Catalog](catalog.json) · [Audit](_checklist.md)\n",
                encoding="utf-8",
            )
            (root / "_checklist.md").write_text(
                "| # | Item | Status |\n"
                "|---|---|---|\n"
                "| 1 | Inventory reconciliation | PASS |\n",
                encoding="utf-8",
            )
            # Passing an execution-contract-shaped wrapper is supported too.
            result = verify_artifact_contract(root, {"output_contract": contract})
            self.assertTrue(result["valid"], result["findings"])

            (root / "rogue.json").write_text("{}", encoding="utf-8")
            mutated = verify_artifact_contract(root, contract)

        self.assertFalse(mutated["valid"])
        self.assertIn("unexpected_artifact", self._codes(mutated))

    def test_format_mapping_must_bind_to_a_declared_artifact(self):
        contract = {
            "declared_artifacts": ["data.json"],
            "artifact_formats": {"dtaa.json": "json"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data.json").write_text("not valid json", encoding="utf-8")
            result = verify_artifact_contract(root, contract)

        self.assertFalse(result["valid"])
        self.assertIn("artifact_format_declaration_unbound", self._codes(result))

    def test_malformed_declared_yaml_is_rejected(self):
        contract = {
            "declared_artifacts": ["data.yml"],
            "artifact_formats": {"data.yml": "yaml"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data.yml").write_text(": bad: [", encoding="utf-8")
            result = verify_artifact_contract(root, contract)

        self.assertFalse(result["valid"])
        self.assertIn("invalid_yaml_artifact", self._codes(result))

    def test_explicit_non_mapping_formats_are_rejected(self):
        contract = {
            "declared_artifacts": ["data.json"],
            "artifact_formats": ["json"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data.json").write_text("{}", encoding="utf-8")
            result = verify_artifact_contract(root, contract)

        self.assertFalse(result["valid"])
        self.assertIn("artifact_formats_invalid", self._codes(result))

    def test_declared_format_aliases_and_empty_text_artifacts(self):
        valid_contract = {
            "declared_artifacts": ["data.json", "table.csv"],
            "artifact_formats": {
                "data.json": "application/json",
                "table.csv": "text/csv",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data.json").write_text('{"ok": true}', encoding="utf-8")
            (root / "table.csv").write_text("name,value\na,1\n", encoding="utf-8")
            valid = verify_artifact_contract(root, valid_contract)
            self.assertTrue(valid["valid"], valid["findings"])

            (root / "index.html").write_text("", encoding="utf-8")
            empty = verify_artifact_contract(
                root,
                {
                    "declared_artifacts": ["index.html"],
                    "artifact_formats": {"index.html": "text/html"},
                },
            )

        self.assertFalse(empty["valid"])
        self.assertIn("empty_html_artifact", self._codes(empty))

    def test_generic_binary_format_and_manifest_hash_are_supported(self):
        contract = {
            "declared_artifacts": ["payload.bin"],
            "artifact_formats": {"payload.bin": "application/octet-stream"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "payload.bin").write_bytes(b"payload")
            result = verify_artifact_contract(root, contract)

        self.assertTrue(result["valid"], result["findings"])
        self.assertEqual(64, len(result["manifest"][0]["sha256"]))

    def test_structured_output_scan_entry_limit_fails_closed(self):
        contract = {
            "declared_artifacts": ["result.json"],
            "artifact_formats": {"result.json": "application/json"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "result.json").write_text('{"ok": true}', encoding="utf-8")
            (root / "unrelated.txt").write_text("input", encoding="utf-8")
            with patch("artifact_contract._MAX_WORKSPACE_SCAN_ENTRIES", 1):
                result = verify_artifact_contract(root, contract)

        self.assertFalse(result["valid"])
        self.assertIn("workspace_scan_entry_limit_exceeded", self._codes(result))
        self.assertEqual([], result["manifest"])
        self.assertEqual(0, result["metrics"]["workspace_scan"]["complete"])

    def test_structured_output_scan_depth_and_path_budgets_fail_closed(self):
        contract = {
            "declared_artifacts": ["nested/deeper/result.json"],
            "artifact_formats": {
                "nested/deeper/result.json": "application/json",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "nested" / "deeper" / "result.json"
            target.parent.mkdir(parents=True)
            target.write_text('{"ok": true}', encoding="utf-8")
            with patch("artifact_contract._MAX_WORKSPACE_SCAN_DEPTH", 2):
                too_deep = verify_artifact_contract(root, contract)
            with patch("artifact_contract._MAX_WORKSPACE_SCAN_PATH_BYTES", 8):
                too_many_path_bytes = verify_artifact_contract(root, contract)

        self.assertIn("workspace_scan_depth_limit_exceeded", self._codes(too_deep))
        self.assertEqual([], too_deep["manifest"])
        self.assertIn(
            "workspace_scan_path_budget_exceeded",
            self._codes(too_many_path_bytes),
        )
        self.assertEqual([], too_many_path_bytes["manifest"])

    def test_structured_output_scan_rejects_but_never_follows_symlink(self):
        contract = {
            "declared_artifacts": ["result.json"],
            "artifact_formats": {"result.json": "application/json"},
        }
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            outside = Path(outside_dir)
            (root / "result.json").write_text('{"ok": true}', encoding="utf-8")
            for index in range(5):
                (outside / f"outside-{index}.txt").write_text("secret", encoding="utf-8")
            try:
                (root / "external").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            # The root itself has two entries. Following the link would exceed
            # this budget, so absence of a scan-limit error proves no traversal.
            with patch("artifact_contract._MAX_WORKSPACE_SCAN_ENTRIES", 2):
                result = verify_artifact_contract(root, contract)

        self.assertFalse(result["valid"])
        self.assertIn("artifact_symlink_rejected", self._codes(result))
        self.assertNotIn("workspace_scan_entry_limit_exceeded", self._codes(result))
        self.assertEqual(["result.json"], [item["path"] for item in result["manifest"]])

    def test_openxml_and_pdf_binary_signatures_are_verified(self):
        contract = {
            "declared_artifacts": ["model.xlsx", "source.pdf"],
            "artifact_formats": {
                "model.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "source.pdf": "application/pdf",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with zipfile.ZipFile(root / "model.xlsx", "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("xl/workbook.xml", "<workbook/>")
            (root / "source.pdf").write_bytes(b"%PDF-1.7\nfixture\n%%EOF")
            valid = verify_artifact_contract(root, contract)

            (root / "model.xlsx").write_bytes(b"not-a-workbook")
            invalid = verify_artifact_contract(root, contract)

        self.assertTrue(valid["valid"], valid["findings"])
        self.assertFalse(invalid["valid"])
        self.assertIn("invalid_xlsx_artifact", self._codes(invalid))

    def test_opaque_mime_format_records_nonempty_hash_and_mime(self):
        contract = {
            "declared_artifacts": ["payload.custom"],
            "artifact_formats": {"payload.custom": "application/x-unknown-fixture"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "payload.custom").write_bytes(b"payload")
            result = verify_artifact_contract(root, contract)

        self.assertTrue(result["valid"], result["findings"])
        metric = result["metrics"]["formats"]["payload.custom"]
        self.assertEqual("application/x-unknown-fixture", metric["mime_type"])
        self.assertEqual(7, metric["bytes"])
        self.assertEqual(64, len(metric["sha256"]))

    def test_kanban_json_business_todo_is_not_a_completion_placeholder(self):
        contract = {
            "declared_artifacts": ["kanban.json"],
            "artifact_formats": {"kanban.json": "json"},
            "quality_contract": {"forbid_pending_markers": True},
        }
        payload = {
            "board": "TODO product launch",
            "columns": [
                {"name": "TODO", "cards": [{"title": "Research TODO APIs"}]},
                {"name": "Done", "cards": []},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "kanban.json").write_text(json.dumps(payload), encoding="utf-8")
            result = verify_artifact_contract(root, contract)

        self.assertTrue(result["valid"], result["findings"])
        self.assertNotIn("pending_completion_marker", self._codes(result))

    def test_pending_marker_scan_requires_explicit_quality_policy(self):
        contract = {"declared_artifacts": ["notes.md"]}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "notes.md").write_text(
                "# Vocabulary\n\nTODO is a workflow column name.\n",
                encoding="utf-8",
            )
            ordinary = verify_artifact_contract(root, contract)
            explicit = verify_artifact_contract(
                root,
                {
                    **contract,
                    "quality_contract": {"forbid_pending_markers": True},
                },
            )

        self.assertTrue(ordinary["valid"], ordinary["findings"])
        self.assertFalse(explicit["valid"])
        self.assertIn("pending_completion_marker", self._codes(explicit))

    def test_json_checklist_uses_structured_statuses_not_markdown_columns(self):
        contract = {
            "declared_ancillary_files": ["checklist.json"],
            "artifact_formats": {"checklist.json": "application/json"},
            "quality_contract": {
                "checklist": {
                    "file": "checklist.json",
                    "format": "json",
                    "rows_field": "checks",
                    "status_field": "complete",
                    "rows": 2,
                    "require_status": True,
                    "accepted_statuses": ["closed"],
                    "pending_markers": True,
                },
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "checklist.json").write_text(
                json.dumps(
                    {
                        "checks": [
                            {
                                "id": "schema",
                                "description": "Preserve the TODO swimlane label",
                                "complete": True,
                            },
                            {"id": "links", "complete": "closed"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            quality = contract.pop("quality_contract")
            result = verify_artifact_contract(
                root,
                {"output_contract": contract, "quality_contract": quality},
            )

        self.assertTrue(result["valid"], result["findings"])
        self.assertEqual("json", result["metrics"]["checklist"]["format"])
        self.assertEqual(2, result["metrics"]["checklist"]["rows"])
        self.assertNotIn("checklist_status_column_missing", self._codes(result))

    def test_checklist_filename_alone_does_not_enable_quality_rules(self):
        contract = {
            "declared_artifacts": ["checklist.json"],
            "artifact_formats": {"checklist.json": "json"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "checklist.json").write_text(
                json.dumps({"tasks": [{"status": "TODO", "title": "backlog"}]}),
                encoding="utf-8",
            )
            result = verify_artifact_contract(root, contract)

        self.assertTrue(result["valid"], result["findings"])
        self.assertNotIn("checklist", result["metrics"])

    def test_svg_html_and_csv_are_validated_by_declared_format(self):
        contract = {
            "declared_artifacts": ["diagram.svg", "index.html", "table.csv"],
            "artifact_formats": {
                "diagram.svg": "image/svg+xml",
                "index.html": "text/html",
                "table.csv": "text/csv",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "diagram.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>',
                encoding="utf-8",
            )
            (root / "index.html").write_text(
                "<!doctype html><html><title>Map</title><body>Ready</body></html>",
                encoding="utf-8",
            )
            (root / "table.csv").write_text("name,value\nalpha,1\n", encoding="utf-8")
            valid = verify_artifact_contract(root, contract)

            (root / "diagram.svg").write_text("<svg><broken></svg>", encoding="utf-8")
            (root / "index.html").write_text("plain text only", encoding="utf-8")
            (root / "table.csv").write_text('name,value\n"unterminated,1', encoding="utf-8")
            invalid = verify_artifact_contract(root, contract)

        self.assertTrue(valid["valid"], valid["findings"])
        self.assertFalse(invalid["valid"])
        self.assertTrue(
            {"invalid_svg_artifact", "invalid_html_artifact", "invalid_csv_artifact"}
            <= self._codes(invalid),
            invalid["findings"],
        )

    def test_exact_set_can_scope_outputs_with_baseline_and_receipts(self):
        contract = {
            "declared_artifacts": ["result.json"],
            "declared_file_count": 1,
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": ["result.json"],
            },
            "artifact_formats": {"result.json": "json"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "input.csv").write_text("id\n1\n", encoding="utf-8")
            (root / "result.json").write_text('{"ok": true}', encoding="utf-8")
            snapshot_only = verify_artifact_contract(root, contract)
            scoped = verify_artifact_contract(
                root,
                contract,
                baseline_paths=["input.csv"],
                receipt_paths=["result.json"],
            )
            stale = verify_artifact_contract(
                root,
                contract,
                baseline_paths=["input.csv", "result.json"],
                receipt_paths=[],
            )

        self.assertFalse(snapshot_only["valid"])
        self.assertIn("unexpected_artifact", self._codes(snapshot_only))
        self.assertTrue(scoped["valid"], scoped["findings"])
        self.assertEqual("run_outputs", scoped["metrics"]["artifact_set_scope"])
        self.assertFalse(stale["valid"])
        self.assertIn("declared_artifact_not_produced", self._codes(stale))

    def test_declarative_validator_checks_hash_json_shape_and_never_executes(self):
        payload = b'{"records": []}'
        import hashlib

        digest = hashlib.sha256(payload).hexdigest()
        contract = {
            "declared_artifacts": ["records.blob"],
            "artifact_formats": {"records.blob": "application/x-record-bundle"},
            "artifact_validators": {
                "records.blob": {
                    "format": "json",
                    "mime_type": "application/x-record-bundle",
                    "sha256": digest,
                    "json_root_type": "object",
                    "required_json_keys": ["records"],
                    "min_bytes": 2,
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "records.blob").write_bytes(payload)
            valid = verify_artifact_contract(root, contract)
            (root / "records.blob").write_bytes(b'{"other": true}')
            invalid = verify_artifact_contract(root, contract)

        self.assertTrue(valid["valid"], valid["findings"])
        self.assertEqual(digest, valid["metrics"]["validators"]["records.blob"]["sha256"])
        self.assertFalse(invalid["valid"])
        self.assertIn("artifact_validator_sha256_failed", self._codes(invalid))
        self.assertIn("artifact_validator_required_json_keys_failed", self._codes(invalid))

    def test_declarative_validator_rejects_unsupported_executable_fields(self):
        contract = {
            "declared_artifacts": ["records.json"],
            "artifact_validators": {
                "records.json": {
                    "format": "json",
                    "command": "python validate.py",
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "records.json").write_text("{}", encoding="utf-8")
            result = verify_artifact_contract(root, contract)

        self.assertFalse(result["valid"])
        self.assertIn("artifact_validator_field_unsupported", self._codes(result))

    def test_baseline_input_does_not_make_output_glob_ambiguous(self):
        contract = {
            "declared_artifacts": ["*.json"],
            "declared_file_count": 1,
            "artifact_set_policy": {"mode": "exact", "artifacts": ["*.json"]},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "input.json").write_text('{"kind": "input"}', encoding="utf-8")
            (root / "result.json").write_text('{"kind": "output"}', encoding="utf-8")
            result = verify_artifact_contract(
                root,
                contract,
                baseline_paths=["input.json"],
                receipt_paths=["result.json"],
            )

        self.assertTrue(result["valid"], result["findings"])
        self.assertNotIn("declared_artifact_ambiguous", self._codes(result))

    def test_root_declaration_is_not_satisfied_by_nested_same_name(self):
        contract = {
            "declared_modular_files": ["catalog.json"],
            "declared_file_count": 1,
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": ["catalog.json"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            (nested / "catalog.json").write_text("{}", encoding="utf-8")
            missing = verify_artifact_contract(root, contract)
            self.assertFalse(missing["valid"])
            self.assertIn("declared_artifact_missing", self._codes(missing))

            (root / "catalog.json").write_text("{}", encoding="utf-8")
            duplicate = verify_artifact_contract(root, contract)

        self.assertFalse(duplicate["valid"])
        self.assertNotIn("declared_artifact_ambiguous", self._codes(duplicate))
        self.assertIn("unexpected_artifact", self._codes(duplicate))

    def test_declared_hidden_artifact_is_verified_but_internal_state_is_ignored(self):
        contract = {
            "declared_modular_files": [".catalog.json"],
            "declared_file_count": 1,
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": [".catalog.json"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".catalog.json").write_text(
                json.dumps({"status": "verified"}),
                encoding="utf-8",
            )
            internal = root / ".chatds"
            internal.mkdir()
            (internal / "runtime.json").write_text("{}", encoding="utf-8")
            result = verify_artifact_contract(root, contract)

        self.assertTrue(result["valid"], result["findings"])
        self.assertEqual([".catalog.json"], [item["path"] for item in result["manifest"]])

    def test_exact_set_checks_every_non_internal_file_type(self):
        contract = {
            "declared_modular_files": ["report.md"],
            "declared_file_count": 1,
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": ["report.md"],
            },
        }
        for extra in ("helper.py", "evidence.json", "notes.txt"):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / "report.md").write_text("# Report\nEvidence.\n", encoding="utf-8")
                (root / extra).write_text("support", encoding="utf-8")
                result = verify_artifact_contract(root, contract)
                self.assertFalse(result["valid"])
                self.assertIn("unexpected_artifact", self._codes(result))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "report.md").write_text("# Report\nEvidence.\n", encoding="utf-8")
            support = root / "support"
            support.mkdir()
            (support / "helper.py").write_text("support", encoding="utf-8")
            allowed_contract = {
                **contract,
                "artifact_set_policy": {
                    **contract["artifact_set_policy"],
                    "allowed_additional_patterns": ["support/*.py"],
                },
            }
            result = verify_artifact_contract(root, allowed_contract)
        self.assertTrue(result["valid"], result["findings"])

    def test_allowed_pattern_overlap_does_not_remove_declared_file_from_count(self):
        contract = {
            "declared_artifacts": ["a.json"],
            "declared_file_count": 1,
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": ["a.json"],
                "allowed_additional_patterns": ["*.json"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.json").write_text('{"ok":true}', encoding="utf-8")
            result = verify_artifact_contract(root, contract)

        self.assertTrue(result["valid"], result["findings"])
        self.assertEqual(1, result["metrics"]["observed_deliverable_count"])

    def test_segment_glob_does_not_cross_directory_boundary(self):
        contract = {
            "declared_modular_files": ["reports/*.md"],
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": ["reports/*.md"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "reports" / "a"
            nested.mkdir(parents=True)
            (nested / "report.md").write_text("# Nested\n", encoding="utf-8")
            result = verify_artifact_contract(root, contract)
        self.assertFalse(result["valid"])
        self.assertIn("declared_artifact_missing", self._codes(result))
        self.assertIn("unexpected_artifact", self._codes(result))

    def test_multiple_placeholder_matches_are_ambiguous(self):
        contract = {
            "declared_final_artifact": "{PROJECT}_FULL.md",
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": ["{PROJECT}_FULL.md"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "ALPHA_FULL.md").write_text("# Alpha\n", encoding="utf-8")
            (root / "BETA_FULL.md").write_text("# Beta\n", encoding="utf-8")
            result = verify_artifact_contract(root, contract)
        self.assertFalse(result["valid"])
        self.assertIn("declared_artifact_ambiguous", self._codes(result))

    def test_same_basename_at_two_explicit_paths_is_not_collapsed(self):
        contract = {
            "declared_modular_files": ["alpha/report.md", "beta/report.md"],
            "declared_file_count": 2,
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": ["alpha/report.md", "beta/report.md"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for directory in ("alpha", "beta"):
                target = root / directory
                target.mkdir()
                (target / "report.md").write_text(
                    f"# {directory}\nSubstantive evidence.\n",
                    encoding="utf-8",
                )
            result = verify_artifact_contract(root, contract)
        self.assertTrue(result["valid"], result["findings"])

    def test_unsafe_declared_pattern_is_rejected(self):
        contract = {
            "declared_modular_files": ["reports/../report.md"],
            "artifact_set_policy": {
                "mode": "exact",
                "artifacts": ["reports/../report.md"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "report.md").write_text("# Report\n", encoding="utf-8")
            result = verify_artifact_contract(root, contract)
        self.assertFalse(result["valid"])
        self.assertIn("declared_artifact_pattern_invalid", self._codes(result))


if __name__ == "__main__":
    unittest.main()
