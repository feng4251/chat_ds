import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path, PurePosixPath

from skills.loader import (
    _extract_declared_final_markdown_files,
    load_skill_content,
)
from artifact_contract import verify_artifact_contract


def _repository_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "skills_and_refs").is_dir() and (parent / "harness").is_dir():
            return parent
    raise AssertionError("Repository root with skills_and_refs was not found")


class SkillCompilerGeneralizationFixtureTests(unittest.TestCase):
    def _write(self, root: Path, relative_path: str, content: str) -> None:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def _compile(self, skill_md: Path, session_id: str) -> dict:
        loaded = load_skill_content(
            skill_md,
            skill_dir=str(skill_md.parent),
            session_id=session_id,
        )
        execution = loaded.get("execution_contract") or {}
        self.assertEqual(
            execution,
            (loaded.get("workflow_contract") or {}).get("execution_contract"),
        )
        return execution

    def test_legacy_chinese_final_labels_do_not_become_path_prefixes(self):
        cases = {
            "最终报告：`最终报告.md`": ["最终报告.md"],
            "输出文件：`docs/最终报告.md`": ["docs/最终报告.md"],
            "最终文件为最终报告.md。": ["最终报告.md"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    expected,
                    _extract_declared_final_markdown_files(text),
                )

    def test_structured_package_contract_supports_nested_unicode_and_non_markdown_final(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: multilingual-web-package
                description: Build a multilingual web package.
                version: "1.0"
                ---
                # Multilingual web package
                Execute the declared package workflow.
                """,
            )
            self._write(
                root,
                "orchestration/site.yaml",
                """
                orchestrator_id: multilingual-site
                version: "1.0"
                routing_rules:
                  build_site:
                    patterns: ["build.*site"]
                    worker: site-builder
                    default: true
                    requires_full_output: true
                output_contract:
                  declared_artifacts:
                    - path: data/分析.json
                      format: json
                    - docs/001_介绍.md
                  declared_final_artifact: site/index.html
                  declared_file_count: 3
                  merge_mandatory: false
                  artifact_formats:
                    site/index.html: html
                  artifact_set_policy:
                    mode: exact
                    artifacts:
                      - data/分析.json
                      - docs/001_介绍.md
                      - site/index.html
                """,
            )
            self._write(
                root,
                "orchestration/workers/site-builder.yaml",
                """
                worker_id: site-builder
                name: Site Builder
                version: "1.0"
                depends_on: []
                """,
            )
            execution = self._compile(root / "SKILL.md", "structured-package")

        diagnostics = execution.get("diagnostics") or {}
        self.assertEqual([], diagnostics.get("errors"))
        output = execution["output_contract"]
        self.assertEqual(
            ["data/分析.json", "docs/001_介绍.md"],
            output["declared_artifacts"],
        )
        self.assertEqual("site/index.html", output["declared_final_artifact"])
        self.assertIs(output["merge_mandatory"], False)
        self.assertEqual("json", output["artifact_formats"]["data/分析.json"])
        self.assertEqual("html", output["artifact_formats"]["site/index.html"])

    def test_direct_output_and_quality_contracts_are_canonical_without_report_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: warehouse-reconciliation
                version: "2.0"
                description: Reconcile warehouse inventory into a reviewed bundle.
                ---
                Follow the declarative workflow and output contracts.
                """,
            )
            self._write(
                root,
                "orchestration/main.yaml",
                """
                orchestrator_id: warehouse-reconciliation
                version: "2.0"
                workers:
                  collector:
                    file: orchestration/workers/collector.yaml
                  reviewer:
                    file: orchestration/workers/reviewer.yaml
                output_contract:
                  declared_modular_files: [raw.json, normalized.csv]
                  declared_final_artifact: "{RUN}_bundle.txt"
                  declared_file_count: 3
                  sections:
                    - id: inventory
                      title: Inventory
                      order: 1
                      source_worker: collector
                      applicability: Include when inventory reconciliation is requested.
                      key_elements: [source count, rejected rows]
                    - id: review
                      title: Review
                      order: 2
                      source_worker: reviewer
                  merge:
                    mandatory: true
                    input_order: [raw.json, normalized.csv]
                    separator: "\\n---\\n"
                    output: "{RUN}_bundle.txt"
                    post_merge_checks: [exact input order]
                quality_contract:
                  narrative_rules: [Distinguish observed and inferred values.]
                  required_markers: ["source_count", "review_status"]
                  required_section_ids: [inventory, review]
                  constraints:
                    max_total_lines: 80
                  section_file_mapping:
                    - file: raw.json
                      section_ids: [inventory]
                    - file: normalized.csv
                      section_ids: [review]
                """,
            )
            for worker in ("collector", "reviewer"):
                self._write(
                    root,
                    f"orchestration/workers/{worker}.yaml",
                    f"worker_id: {worker}\nversion: '1.0'\ndepends_on: []\n",
                )
            execution = self._compile(root / "SKILL.md", "warehouse-contract")

        self.assertTrue(execution["diagnostics"]["valid"])
        output = execution["output_contract"]
        self.assertEqual(["inventory", "review"], [
            section["id"] for section in output["sections"]
        ])
        self.assertEqual(["collector"], output["sections"][0]["source_workers"])
        self.assertEqual(["raw.json", "normalized.csv"], output["merge_input_order"])
        self.assertEqual("\n---\n", output["merge_separator"])
        self.assertTrue(output["merge_mandatory"])
        self.assertEqual(["exact input order"], output["post_merge_checks"])
        self.assertEqual("{RUN}_bundle.txt", output["declared_final_artifact"])
        self.assertEqual(
            "orchestration/main.yaml",
            output["merge_declarations"][0]["source_file"],
        )
        quality = execution["quality_contract"]
        self.assertEqual(
            ["source_count", "review_status"],
            quality["required_module_markers"],
        )
        self.assertEqual(80, quality["constraints"]["max_total_lines"])
        self.assertEqual(["inventory", "review"], quality["required_section_ids"])
        self.assertEqual(
            ["raw.json", "normalized.csv"],
            [mapping["file"] for mapping in quality["section_file_mapping"]],
        )

    def test_structured_quality_policies_survive_compiler_and_reach_verifier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: reviewed-markdown-package
                description: Produce a reviewed Markdown package.
                ---
                Follow the declarative artifact and quality contracts.
                """,
            )
            self._write(
                root,
                "orchestration/main.yaml",
                """
                orchestrator_id: reviewed-markdown-package
                output_contract:
                  declared_artifacts: [report.md]
                  declared_ancillary_files: [review.json]
                  declared_file_count: 2
                  enforce_exact_section_titles: true
                  verify_first_last_match: false
                  required_markers: [Evidence ID]
                  artifact_formats:
                    report.md: markdown
                    review.json: json
                quality_contract:
                  forbid_pending_markers: true
                  detect_padding: true
                  forbid_duplicate_numbered_headings: true
                  max_module_lines: 250
                  checklist:
                    file: review.json
                    format: json
                    rows: 1
                    status_field: status
                    accepted_statuses: [done]
                """,
            )
            execution = self._compile(
                root / "SKILL.md",
                "structured-quality-policy",
            )
            quality = execution["quality_contract"]
            self.assertTrue(execution["diagnostics"]["valid"], execution["diagnostics"])
            self.assertIs(
                execution["output_contract"]["enforce_exact_section_titles"],
                True,
            )
            self.assertIs(
                execution["output_contract"]["verify_first_last_match"],
                False,
            )
            self.assertEqual(
                ["Evidence ID"],
                execution["output_contract"]["required_markers"],
            )
            self.assertIs(quality["forbid_pending_markers"], True)
            self.assertIs(quality["detect_padding"], True)
            self.assertIs(quality["forbid_duplicate_numbered_headings"], True)
            self.assertEqual(250, quality["max_module_lines"])
            self.assertEqual("review.json", quality["checklist"]["file"])

            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "report.md").write_text(
                "# Report\n\nTODO: finish evidence reconciliation.\n",
                encoding="utf-8",
            )
            (workspace / "review.json").write_text(
                '{"rows":[{"status":"pending"}]}',
                encoding="utf-8",
            )
            verification = verify_artifact_contract(
                workspace,
                {
                    "output_contract": execution["output_contract"],
                    "quality_contract": quality,
                },
            )

        codes = {
            finding.get("code")
            for finding in verification["findings"]
            if isinstance(finding, dict)
        }
        self.assertIn("pending_completion_marker", codes)
        self.assertIn("checklist_status_invalid", codes)

    def test_malformed_structured_quality_completion_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: malformed-quality\ndescription: Reject malformed quality policy.\n---\nBuild it.\n",
            )
            self._write(
                root,
                "orchestration/main.yaml",
                """
                orchestrator_id: malformed-quality
                output_contract:
                  declared_artifacts: [report.md]
                quality_contract:
                  forbid_pending_markers: yes
                  checklist: [review.json]
                  padding_policy: enabled
                  checklist_row_mode: approximately
                """,
            )
            execution = self._compile(
                root / "SKILL.md",
                "malformed-quality-policy",
            )

        codes = {
            item.get("code")
            for item in execution["diagnostics"]["errors"]
            if isinstance(item, dict)
        }
        self.assertIn("invalid_quality_contract_field", codes)
        self.assertFalse(execution["diagnostics"]["valid"])

    def test_structured_empty_modules_block_legacy_format_backfill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: final-only-package
                description: Compile a final-only artifact package.
                version: "1.0"
                ---
                Build the declared final-only package.
                """,
            )
            self._write(
                root,
                "orchestration/main.yaml",
                """
                orchestrator_id: final-only
                version: "1.0"
                output_contract:
                  declared_modular_files: []
                  declared_final_artifact: site/index.html
                  declared_file_count: 1
                  merge_mandatory: false
                """,
            )
            self._write(
                root,
                "formats/legacy.md",
                """
                # Old example, no longer authoritative
                1 content file + `FULL_REPORT.md` = 2 total files.
                | File | Section |
                |---|---|
                | `001_old.md` | Old example |
                """,
            )
            execution = self._compile(root / "SKILL.md", "final-only")

        output = execution["output_contract"]
        self.assertNotIn("declared_modular_files", output)
        self.assertEqual("site/index.html", output["declared_final_artifact"])
        self.assertEqual(1, output["declared_file_count"])

    def test_unknown_structured_artifact_policy_mode_is_a_compiler_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: bad-policy\ndescription: Reject an unknown artifact policy.\nversion: '1.0'\n---\nBuild it.\n",
            )
            self._write(
                root,
                "orchestration/main.yaml",
                """
                orchestrator_id: bad-policy
                version: "1.0"
                output_contract:
                  declared_artifacts: [data.json]
                  artifact_set_policy:
                    mode: excat
                    artifacts: [data.json]
                """,
            )
            execution = self._compile(root / "SKILL.md", "bad-policy")

        codes = {
            item.get("code")
            for item in execution["diagnostics"]["errors"]
        }
        self.assertIn("invalid_artifact_set_policy_mode", codes)
        self.assertFalse(execution["diagnostics"]["valid"])

    def test_malformed_structured_format_and_policy_fields_fail_closed(self):
        cases = (
            ("artifact_formats: [json]", "invalid_package_artifact_formats"),
            ("artifact_set_policy: true", "invalid_artifact_set_policy"),
            ("merge_mandatory: 'true'", "invalid_package_output_contract_field"),
            ("expected_min_bytes: large", "invalid_package_output_contract_field"),
            ("artifact_index: []", "invalid_package_artifact_index"),
            (
                "artifact_set_policy:\n  mode: ''\n  artifacts: [data.json]",
                "invalid_artifact_set_policy_mode",
            ),
            (
                "artifact_set_policy:\n  mode: null\n  artifacts: [data.json]",
                "invalid_artifact_set_policy_mode",
            ),
        )
        for index, (fragment, expected_code) in enumerate(cases):
            with self.subTest(fragment=fragment):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    self._write(
                        root,
                        "SKILL.md",
                        "---\nname: malformed-package\ndescription: Reject malformed structured format declarations.\nversion: '1.0'\n---\nBuild it.\n",
                    )
                    self._write(
                        root,
                        "orchestration/main.yaml",
                        (
                            f"orchestrator_id: malformed-package-{index}\n"
                            'version: "1.0"\n'
                            "output_contract:\n"
                            "  declared_artifacts: [data.json]\n"
                            f"{textwrap.indent(fragment, '  ')}\n"
                        ),
                    )
                    execution = self._compile(
                        root / "SKILL.md",
                        f"malformed-package-{index}",
                    )

                codes = {
                    item.get("code")
                    for item in execution["diagnostics"]["errors"]
                }
                self.assertIn(expected_code, codes)
                self.assertFalse(execution["diagnostics"]["valid"])

    def test_compatible_package_contracts_across_orchestrators_merge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: compatible-package\ndescription: Merge compatible package contracts.\nversion: '1.0'\n---\nBuild it.\n",
            )
            self._write(
                root,
                "orchestration/a.yaml",
                """
                orchestrator_id: a
                version: "1.0"
                output_contract:
                  declared_artifacts: [data.json, table.csv]
                  artifact_formats: {data.json: json}
                """,
            )
            self._write(
                root,
                "orchestration/b.yaml",
                """
                orchestrator_id: b
                version: "1.0"
                output_contract:
                  declared_artifacts: [data.json, table.csv]
                  artifact_formats: {table.csv: csv}
                """,
            )
            execution = self._compile(root / "SKILL.md", "compatible-package")

        self.assertEqual([], execution["diagnostics"]["errors"])
        self.assertEqual(
            {"data.json": "json", "table.csv": "csv"},
            execution["output_contract"]["artifact_formats"],
        )

    def test_conflicting_package_contracts_across_orchestrators_fail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: conflicting-package\ndescription: Reject conflicting package contracts.\nversion: '1.0'\n---\nBuild it.\n",
            )
            for filename, artifact in (("a.yaml", "a.json"), ("b.yaml", "b.json")):
                self._write(
                    root,
                    f"orchestration/{filename}",
                    f"""
                    orchestrator_id: {filename[:-5]}
                    version: "1.0"
                    output_contract:
                      declared_artifacts: [{artifact}]
                    """,
                )
            execution = self._compile(root / "SKILL.md", "conflicting-package")

        errors = execution["diagnostics"]["errors"]
        self.assertTrue(any(
            item.get("code") == "conflicting_structured_output_contract"
            and item.get("field") == "output_contract.declared_artifacts"
            for item in errors
        ), errors)
        self.assertFalse(execution["diagnostics"]["valid"])

    def test_legacy_markdown_table_preserves_number_width_nested_unicode_and_un_numbered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: legacy-unicode-package
                description: Preserve legacy Unicode artifact paths.
                version: "1.0"
                ---
                # Legacy package
                Use the declared output format.
                """,
            )
            self._write(
                root,
                "orchestration/main.yaml",
                """
                orchestrator_id: legacy-unicode
                version: "1.0"
                routing_rules:
                  default_route:
                    patterns: ["package"]
                    worker: writer
                    default: true
                    requires_full_output: true
                    format_files: [formats/package.md]
                """,
            )
            self._write(
                root,
                "orchestration/workers/writer.yaml",
                """
                worker_id: writer
                version: "1.0"
                depends_on: []
                """,
            )
            self._write(
                root,
                "formats/package.md",
                """
                # Package format
                3 content files + `README.md` (index) + `FULL_REPORT.md` = 5 total.

                | File | Sections |
                |---|---|
                | `docs/001_intro.md` | Intro |
                | `docs/02_结论.md` | Conclusion |
                | `analysis.md` | Analysis |
                """,
            )
            execution = self._compile(root / "SKILL.md", "legacy-unicode")

        self.assertEqual(
            ["docs/001_intro.md", "docs/02_结论.md", "analysis.md"],
            execution["output_contract"]["declared_modular_files"],
        )

    def test_legacy_unicode_final_report_label_is_not_ancillary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: legacy-chinese-final
                description: Preserve a legacy Chinese final report label.
                version: "1.0"
                ---
                Follow the declared output format.
                """,
            )
            self._write(
                root,
                "orchestration/main.yaml",
                """
                orchestrator_id: legacy-chinese-final
                version: "1.0"
                routing_rules:
                  default_route:
                    patterns: ["package"]
                    worker: writer
                    default: true
                    requires_full_output: true
                    format_files: [formats/package.md]
                """,
            )
            self._write(
                root,
                "orchestration/workers/writer.yaml",
                """
                worker_id: writer
                version: "1.0"
                depends_on: []
                """,
            )
            self._write(
                root,
                "formats/package.md",
                """
                # 输出格式
                2 个内容文件 + `最终报告.md`（最终合并报告） = 3 total files.

                | 文件 | 章节 |
                |---|---|
                | `01_介绍.md` | 介绍 |
                | `02_结论.md` | 结论 |
                """,
            )
            execution = self._compile(root / "SKILL.md", "legacy-cn-final")

        output = execution["output_contract"]
        self.assertEqual("最终报告.md", output["declared_final_artifact"])
        self.assertNotIn(
            "最终报告.md",
            output.get("declared_ancillary_files") or [],
        )

    def test_legacy_auto_merge_mandatory_is_a_three_state_declaration(self):
        cases = (
            ("", None, True),
            ("mandatory: false", False, False),
            ("mandatory: true", True, True),
        )
        for index, (mandatory_line, expected_flag, expected_requires) in enumerate(cases):
            with self.subTest(mandatory=expected_flag), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self._write(
                    root,
                    "SKILL.md",
                    f"---\nname: merge-tristate-{index}\ndescription: Exercise three-state merge declarations.\nversion: '1.0'\n---\nRun it.\n",
                )
                self._write(
                    root,
                    "orchestration/main.yaml",
                    f"""
                    orchestrator_id: merge-tristate-{index}
                    version: "1.0"
                    final_report_template:
                      auto_merge:
                        {mandatory_line}
                        command: "cat 01.md 02.md > FINAL.md"
                        output_artifact: FINAL.md
                    """,
                )
                loaded = load_skill_content(
                    root / "SKILL.md",
                    skill_dir=str(root),
                    session_id=f"merge-tristate-{index}",
                )
                workflow = loaded["workflow_contract"]
                output = workflow["execution_contract"]["output_contract"]
                if expected_flag is None:
                    self.assertNotIn("merge_mandatory", output)
                else:
                    self.assertIs(output["merge_mandatory"], expected_flag)
                self.assertIs(
                    bool(workflow.get("requires_merge")),
                    expected_requires,
                )

    def test_fixed_v23_zip_compiles_declarative_contract_without_ground_truth(self):
        try:
            repository_root = _repository_root()
        except AssertionError:
            # The production harness image intentionally contains only the
            # harness build context, not repository-level reference archives.
            # Source-tree CI still executes this exact ZIP fixture; image-level
            # tests retain the two self-contained cross-domain holdouts below.
            self.skipTest("repository reference archives are not packaged in this image")
        archive_path = (
            repository_root
            / "skills_and_refs"
            / "xClinicalTrial-Design-V2.3.zip"
        )
        self.assertTrue(archive_path.is_file(), archive_path)

        with tempfile.TemporaryDirectory() as temp_dir:
            extraction_root = Path(temp_dir)
            with zipfile.ZipFile(archive_path) as archive:
                # Keep the regression fixture safe even if the zip is replaced.
                for member in archive.infolist():
                    member_path = PurePosixPath(member.filename)
                    self.assertFalse(member_path.is_absolute(), member.filename)
                    self.assertNotIn("..", member_path.parts, member.filename)
                archive.extractall(extraction_root)

            skill_md = extraction_root / "trial-artifs-sim" / "SKILL.md"
            self.assertTrue(skill_md.is_file())
            execution = self._compile(skill_md, "fixed-v23-zip")

        diagnostics = execution.get("diagnostics") or {}
        self.assertEqual(0, (diagnostics.get("summary") or {}).get("error_count"))
        self.assertTrue(diagnostics.get("valid"))
        self.assertEqual("healthsim-trialsim", execution["metadata"]["skill_name"])
        self.assertEqual(9, len(execution.get("workers") or []))
        self.assertEqual(17, len(execution.get("routes") or []))
        self.assertEqual(
            5,
            len((execution.get("intent_classification") or {}).get("dimensions") or []),
        )
        self.assertEqual(
            7,
            len((execution.get("knowledge_bootstrap") or {}).get("sources") or []),
        )
        self.assertEqual(
            6,
            len((execution.get("aggregation") or {}).get("steps") or []),
        )
        self.assertEqual(
            13,
            len((execution.get("aggregation") or {}).get("checks") or []),
        )
        self.assertEqual(
            6,
            len((execution.get("conflict_resolution") or {}).get("strategies") or []),
        )

        routes = {route["id"]: route for route in execution.get("routes") or []}
        full_route = routes["composite_full_protocol_design"]
        self.assertEqual(7, len(full_route["parallel_workers"]))
        self.assertEqual(["worker-ie-criteria"], full_route["sequential_workers"])
        self.assertEqual(
            ["parallel", "sequential"],
            [wave["mode"] for wave in full_route["waves"]],
        )

        output = execution.get("output_contract") or {}
        self.assertEqual(14, output.get("declared_file_count"))
        self.assertEqual(11, len(output.get("declared_modular_files") or []))
        self.assertEqual(20, output.get("declared_section_count"))
        self.assertEqual(153_600, output.get("expected_min_bytes"))
        self.assertEqual(256_000, output.get("expected_max_bytes"))
        self.assertEqual(2_000, output.get("expected_min_lines"))

    def test_satellite_holdout_compiles_parallel_then_sequential_exact_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: satellite-readiness-board
                version: "1.4"
                description: Coordinate a satellite launch-readiness review.
                ---
                # Satellite Readiness Board
                Follow the declarative mission review workflow.
                """,
            )
            self._write(
                root,
                "orchestration/mission.yaml",
                """
                orchestrator_id: satellite-readiness
                name: Satellite Readiness
                version: "1.4"
                intent_classification:
                  dimensions:
                    review_scope:
                      values: [telemetry_only, full_launch_review]
                      workers_map:
                        telemetry_only: [orbit-analyst]
                        full_launch_review: [orbit-analyst, risk-reviewer, flight-director]
                knowledge_bootstrap:
                  pre_fetch_sources:
                    - name: mission-telemetry
                      tool: telemetry_search
                    - name: weather-window
                      tool: weather_search
                routing_rules:
                  telemetry_lookup:
                    patterns: ["inspect.*telemetry"]
                    worker: orbit-analyst
                    spawn_mode: direct
                  launch_readiness:
                    patterns: ["review.*launch.*readiness"]
                    workers: [orbit-analyst, risk-reviewer]
                    sequential_workers: [flight-director]
                    spawn_mode: parallel
                    default: true
                    requires_full_output: true
                    format_files: [formats/board-package.md]
                aggregation:
                  steps:
                    - step: NORMALIZE_FINDINGS
                    - step: GO_NO_GO
                      checks:
                        - id: telemetry_consistent
                        - id: risks_resolved
                conflict_resolution:
                  strategies:
                    - name: SAFETY_FIRST
                      priority: 1
                    - name: ESCALATE_UNRESOLVED
                      priority: 2
                final_report_template:
                  sections:
                    - section: Mission Evidence
                      order: 1
                      source_worker: orbit-analyst
                    - section: Risk Register
                      order: 2
                      source_worker: risk-reviewer
                    - section: Launch Decision
                      order: 3
                      source_worker: flight-director
                  auto_merge:
                    mandatory: true
                    command_template: "cat 01_*.md 02_*.md > {MISSION}_BOARD.md"
                    output_artifact: "{MISSION}_BOARD.md"
                    expected_size_range: "1KB-5KB"
                    post_merge_verification:
                      - "Line count > 20"
                """,
            )
            for worker_id, dependencies in (
                ("orbit-analyst", []),
                ("risk-reviewer", []),
                ("flight-director", ["orbit-analyst", "risk-reviewer"]),
            ):
                dependency_text = ", ".join(dependencies)
                self._write(
                    root,
                    f"orchestration/workers/{worker_id}.yaml",
                    f"""
                    worker_id: {worker_id}
                    name: {worker_id.replace('-', ' ').title()}
                    version: "1.0"
                    depends_on: [{dependency_text}]
                    knowledge_gate:
                      checks: [mission data available]
                    """,
                )
            self._write(
                root,
                "formats/board-package.md",
                """
                # Board package

                2 content files + `README.md` (index) + `_checklist.md` + `FULL_REPORT.md` = 5 total.
                Exactly 5 files are allowed; no additional artifacts are allowed.

                | File | Sections |
                |---|---|
                | `01_mission_evidence.md` | Mission Evidence + Risk Register |
                | `02_launch_decision.md` | Launch Decision |

                Max lines per file | 120

                ```markdown
                # Module
                **Owner**: [worker]
                **Decision**: [decision]
                ```

                Every file must include the declared owner and decision markers.
                """,
            )
            execution = self._compile(root / "SKILL.md", "satellite-holdout")

        self.assertEqual([], (execution["diagnostics"] or {}).get("errors"))
        self.assertEqual(
            ["orbit-analyst", "risk-reviewer", "flight-director"],
            next(
                route for route in execution["routes"]
                if route["id"] == "launch_readiness"
            )["workers"],
        )
        waves = next(
            route for route in execution["routes"]
            if route["id"] == "launch_readiness"
        )["waves"]
        self.assertEqual(["parallel", "sequential"], [wave["mode"] for wave in waves])
        self.assertEqual(2, len(execution["knowledge_bootstrap"]["sources"]))
        self.assertEqual(2, len(execution["aggregation"]["steps"]))
        self.assertEqual(2, len(execution["aggregation"]["checks"]))
        output = execution["output_contract"]
        self.assertEqual(5, output["declared_file_count"])
        self.assertEqual(2, len(output["declared_modular_files"]))
        self.assertEqual("exact", output["artifact_set_policy"]["mode"])
        self.assertEqual("{MISSION}_BOARD.md", output["declared_final_artifact"])

    def test_museum_holdout_compiles_sequential_json_route_without_report_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: museum-catalog-reconciliation
                version: "7.0"
                description: Reconcile collection inventory and provenance records.
                ---
                # Museum Catalog Reconciliation
                Use the declared catalog workflow.
                """,
            )
            self._write(
                root,
                "workflows/catalog.yaml",
                """
                orchestrator_id: museum-catalog
                name: Museum Catalog
                version: "7.0"
                intent_classification:
                  dimensions:
                    operation:
                      values: [lookup, reconcile]
                      workers_map:
                        lookup: [catalog-reader]
                        reconcile: [catalog-reader, provenance-checker]
                routing_rules:
                  object_lookup:
                    patterns: ["lookup.*object"]
                    worker: catalog-reader
                    spawn_mode: direct
                    deliverable:
                      type: json
                      canonical: object.json
                  collection_reconcile:
                    patterns: ["reconcile.*collection"]
                    workers: [catalog-reader, provenance-checker]
                    spawn_mode: sequential
                    default: true
                    requires_full_output: false
                    deliverable:
                      type: json
                      canonical: reconciliation.json
                aggregation:
                  steps:
                    - step: RECONCILE_IDENTIFIERS
                      checks:
                        - id: accessions_unique
                """,
            )
            self._write(
                root,
                "workflows/workers/catalog-reader.yaml",
                """
                worker_id: catalog-reader
                name: Catalog Reader
                version: "1.0"
                depends_on: []
                """,
            )
            self._write(
                root,
                "workflows/workers/provenance-checker.yaml",
                """
                worker_id: provenance-checker
                name: Provenance Checker
                version: "1.0"
                depends_on: [catalog-reader]
                """,
            )
            execution = self._compile(root / "SKILL.md", "museum-holdout")

        self.assertEqual([], (execution["diagnostics"] or {}).get("errors"))
        self.assertEqual(
            ["catalog-reader", "provenance-checker"],
            execution["worker_ids"],
        )
        routes = {route["id"]: route for route in execution["routes"]}
        self.assertEqual("direct", routes["object_lookup"]["spawn_mode"])
        self.assertEqual("sequential", routes["collection_reconcile"]["spawn_mode"])
        self.assertFalse(routes["collection_reconcile"]["requires_full_output"])
        self.assertEqual(
            {"type": "json", "canonical": "reconciliation.json"},
            routes["collection_reconcile"]["deliverable"],
        )
        self.assertEqual(
            {
                "declared_artifacts": ["reconciliation.json"],
                "declared_file_count": 1,
                "artifact_formats": {"reconciliation.json": "json"},
                "route_scoped": True,
            },
            routes["collection_reconcile"]["output_contract"],
        )
        self.assertEqual(1, len(execution["aggregation"]["steps"]))
        self.assertEqual(1, len(execution["aggregation"]["checks"]))
        self.assertNotIn("output_contract", execution)


if __name__ == "__main__":
    unittest.main()
