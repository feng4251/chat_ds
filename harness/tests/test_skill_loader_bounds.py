import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills.loader import (
    FrontmatterParseError,
    _extract_artifact_patterns,
    _extract_external_sources,
    _extract_merge_requirements,
    load_skill_content,
    parse_frontmatter,
)


class SkillLoaderBoundsTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _base_skill(self, root: Path) -> None:
        self._write(
            root,
            "SKILL.md",
            "---\nname: bounded-skill\ndescription: bounded fixture\n---\n# Skill\n",
        )

    def _error_codes(self, loaded: dict) -> set[str]:
        execution = loaded.get("execution_contract") or {}
        diagnostics = execution.get("diagnostics") or loaded.get("package_diagnostics") or {}
        return {
            str(item.get("code"))
            for item in diagnostics.get("errors") or []
            if isinstance(item, dict)
        }

    def _warning_codes(self, loaded: dict) -> set[str]:
        diagnostics = loaded.get("package_diagnostics") or {}
        return {
            str(item.get("code"))
            for item in diagnostics.get("warnings") or []
            if isinstance(item, dict)
        }

    def test_legacy_artifact_inference_requires_explicit_output_context(self):
        instruction_only = (
            "Read output.json and explain it inline. Do not create files.\n"
            "Inputs: 01_report.md, *.csv. Compare these 3 files in chat.\n"
            "Use references/report_template.md as the source template.\n"
        )
        declared_outputs = (
            "Final output: result.json\n"
            "Generate `summary.html` and `metrics.csv`.\n"
            "Save the rendered view to deliverables/dashboard.svg.\n"
            "This produces 4 output files.\n"
        )

        self.assertEqual([], _extract_artifact_patterns(instruction_only))
        self.assertEqual(
            [
                "result.json",
                "summary.html",
                "metrics.csv",
                "deliverables/dashboard.svg",
                "declared_file_count:4",
            ],
            _extract_artifact_patterns(declared_outputs),
        )

    def test_negated_full_report_phrase_does_not_invent_merge_contract(self):
        self.assertEqual(
            [],
            _extract_merge_requirements(
                "Do not merge files and do not create a full report; return "
                "the answer inline."
            ),
        )
        self.assertEqual(
            [],
            _extract_merge_requirements(
                "The reference schema contains a field named full_report."
            ),
        )
        self.assertEqual(
            ["You MUST merge the output files into the final report."],
            _extract_merge_requirements(
                "You MUST merge the output files into the final report."
            ),
        )

    def test_legacy_multiline_output_list_stops_at_boundaries_and_masks_code(self):
        declared = (
            "Output files:\n"
            "- summary.html\n"
            "- data.csv\n"
            "This paragraph discusses input.csv as an input.\n"
            "\n"
            "- after_blank.json\n"
            "## Input files\n"
            "- source.csv\n"
            "```markdown\n"
            "Output files:\n"
            "- fenced-example.md\n"
            "```\n"
            "## Final output\n"
            "- final/result.json\n"
        )

        self.assertEqual(
            ["summary.html", "data.csv", "final/result.json"],
            _extract_artifact_patterns(declared),
        )

    def test_instruction_only_skill_is_not_upgraded_to_artifact_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: inline-review\ndescription: Review supplied data inline\n---\n"
                "Read output.json and compare 01_report.md with 3 CSV files. "
                "Do not create files; answer in chat.\n",
            )

            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        workflow = loaded.get("workflow_contract") or {}
        self.assertNotIn("artifact_patterns", workflow)
        self.assertNotIn("requires_modular_artifacts", workflow)

    def test_format_contract_reads_tail_beyond_presentation_preview_limit(self):
        """Authoritative format declarations must never be prefix-inferred."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: long-format\ndescription: long format fixture\n---\n"
                "# Skill\n\nApply [the output format](formats/output.md).\n",
            )
            # The historical presentation helper stopped at 120,000 chars.
            # Keep this file within the authoritative semantic bound while
            # placing the only output declarations after that old cutoff.
            self._write(
                root,
                "formats/output.md",
                "# Format\n"
                + ("context filler line\n" * 7_000)
                + "\nOutput files:\n"
                "- 01_summary.md\n"
                "- final_report.md\n",
            )

            loaded = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
            )

        self.assertTrue((loaded.get("package_diagnostics") or {}).get("valid"))
        output = (loaded.get("workflow_contract") or {}).get("output_contract") or {}
        self.assertEqual(["01_summary.md"], output.get("declared_modular_files"))
        self.assertEqual("final_report.md", output.get("declared_final_artifact"))
        self.assertEqual(["formats/output.md"], output.get("output_format_files"))

    def test_code_and_tabular_resources_are_not_prose_contracts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: data-lookup\ndescription: inline lookup\n---\n# Lookup\n\n"
                "Use [the declared helper](scripts/example.py) when needed.\n",
            )
            self._write(
                root,
                "scripts/example.py",
                "def example():\n    save('output.json')\n" * 20,
            )
            self._write(
                root,
                "lookup.csv",
                "key,value\n" + "alpha,beta\n" * 100,
            )
            self._write(
                root,
                "lookup.json",
                "{\"records\": [" + "{\"key\": \"value\"}," * 100 + "{}]}",
            )
            with patch("skills.loader.MAX_WORKFLOW_SEMANTIC_FILE_CHARS", 256):
                loaded = load_skill_content(
                    root / "SKILL.md",
                    skill_dir=str(root),
                )

        workflow = loaded.get("workflow_contract") or {}
        self.assertNotIn("artifact_patterns", workflow)
        self.assertNotIn("requires_modular_artifacts", workflow)
        self.assertNotIn(
            "workflow_semantic_file_size_exceeded",
            self._error_codes(loaded),
        )
        self.assertIn("scripts/example.py", workflow.get("script_candidates") or [])

    def test_portable_hub_install_path_binds_selected_package_script(self):
        """Canonical Hub install spellings remain package-local authority."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\n"
                "name: portable-cards\n"
                "description: portable helper fixture\n"
                "---\n"
                "Run `python3 ~/.hermes/skills/productivity/portable-cards/"
                "scripts/cards.py stats` and report the result.\n",
            )
            self._write(
                root,
                "scripts/cards.py",
                "print('ok')\n",
            )

            loaded = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
            )

        workflow = loaded.get("workflow_contract") or {}
        self.assertEqual(
            ["scripts/cards.py"],
            workflow.get("script_candidates"),
        )
        authority = workflow.get("resource_authority") or {}
        self.assertIn(
            "scripts/cards.py",
            authority.get("blocking_resources") or [],
        )
        manifest = loaded.get("runtime_profile_manifest") or {}
        self.assertTrue(manifest.get("valid"), manifest)

    def test_exact_runtime_manifest_declares_content_addressed_entrypoint(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_skill(root)
            self._write(
                root,
                "scripts/dynamic.cjs",
                'const name = "playwright";\nrequire(name);\n',
            )
            self._write(
                root,
                "chatds-runtime.json",
                json.dumps({
                    "schema_version": 1,
                    "entrypoints": [{
                        "path": "scripts/dynamic.cjs",
                        "runtime_profile": "browser-automation-v1",
                        "node_packages": ["playwright"],
                    }],
                }),
            )

            loaded = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
            )

        workflow = loaded.get("workflow_contract") or {}
        manifest = loaded.get("runtime_profile_manifest") or {}
        self.assertTrue(manifest.get("valid"), manifest)
        self.assertEqual(
            ["scripts/dynamic.cjs"],
            workflow.get("script_candidates"),
        )
        self.assertEqual(
            "chatds-runtime.json",
            manifest["entrypoint_manifest"]["path"],
        )
        authority = workflow.get("resource_authority") or {}
        self.assertIn(
            "chatds-runtime.json",
            authority.get("blocking_resources") or [],
        )
        self.assertIn(
            "declared_by:chatds-runtime.json",
            (authority.get("reasons") or {}).get(
                "scripts/dynamic.cjs"
            ) or [],
        )

    def test_recursive_yaml_alias_fails_closed_without_recursion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_skill(root)
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                "orchestrator_id: recursive\n"
                "routing_rules: &routes\n"
                "  recursive_route: *routes\n",
            )

            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        self.assertIn("compiler_structure_cycle", self._error_codes(loaded))

    def test_skill_frontmatter_yaml_errors_and_duplicate_keys_fail_closed(self):
        cases = (
            (
                "---\nname: first\nname: second\ndescription: duplicate\n---\n# Skill\n",
                "duplicate_frontmatter_key",
            ),
            (
                "---\nname: malformed\ndescription: [unterminated\n---\n# Skill\n",
                "invalid_frontmatter_yaml",
            ),
            (
                "---\nname: unclosed\ndescription: missing delimiter\n# Skill\n",
                "unclosed_frontmatter",
            ),
            (
                "# Missing required Agent Skill frontmatter\n",
                "missing_skill_frontmatter",
            ),
        )
        for content, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    self._write(root, "SKILL.md", content)
                    loaded = load_skill_content(
                        root / "SKILL.md",
                        skill_dir=str(root),
                    )
                self.assertIn(expected_code, self._error_codes(loaded))
                self.assertNotIn("execution_contract", loaded)

    def test_standard_manifest_fields_and_block_scalars_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\n"
                "name: archive-review\n"
                "description: >-\n"
                "  Review collection records and reconcile catalog fields.\n"
                "  Use when validating an archive export.\n"
                "license: Apache-2.0\n"
                "compatibility: Requires read access to a catalog export\n"
                "metadata:\n"
                "  author: archive-team\n"
                "  version: \"2.0\"\n"
                "allowed-tools: Read Bash(jq:*)\n"
                "---\n"
                "# Archive review\n",
            )

            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        self.assertNotIn("error", loaded)
        self.assertEqual(
            "Review collection records and reconcile catalog fields. Use when validating an archive export.",
            loaded["description"],
        )
        self.assertEqual("Apache-2.0", loaded["license"])
        self.assertEqual(
            "Requires read access to a catalog export", loaded["compatibility"]
        )
        self.assertEqual(
            {"author": "archive-team", "version": "2.0"},
            loaded["frontmatter"]["metadata"],
        )
        environment = loaded["execution_contract"]["environment_contract"]
        self.assertEqual(
            ["Read", "Bash(jq:*)"],
            environment["allowed_tools"],
        )
        self.assertIn("skill_name_directory_mismatch", self._warning_codes(loaded))

    def test_semantically_invalid_standard_manifest_fields_fail_closed(self):
        cases = (
            ("name: Bad_Name\ndescription: invalid name\n", "invalid_skill_name"),
            ("name: missing-description\n", "missing_skill_description"),
            (
                "name: invalid-description\ndescription: true\n",
                "invalid_skill_description_type",
            ),
            (
                "name: invalid-compatibility\ndescription: fixture\ncompatibility: 3\n",
                "invalid_skill_compatibility_type",
            ),
            (
                "name: invalid-metadata\ndescription: fixture\nmetadata:\n  version: 2\n",
                "invalid_skill_metadata_value",
            ),
        )
        for frontmatter, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    self._write(
                        root,
                        "SKILL.md",
                        f"---\n{frontmatter}---\n# Skill\n",
                    )
                    loaded = load_skill_content(
                        root / "SKILL.md", skill_dir=str(root)
                    )
                self.assertIn(expected_code, self._error_codes(loaded))
                self.assertNotIn("execution_contract", loaded)

    def test_harness_manifest_extensions_are_observable_not_silent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\n"
                "name: staged-skill\n"
                "description: staged compatibility fixture\n"
                "allowed-tools: []\n"
                "metadata:\n"
                "  hermes:\n"
                "    tags: [archive]\n"
                "---\n# Skill\n",
            )
            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        self.assertNotIn("error", loaded)
        self.assertTrue(
            {
                "nonstandard_allowed_tools_sequence",
                "nonstandard_namespaced_metadata_extension",
            }.issubset(self._warning_codes(loaded))
        )

    def test_unreferenced_generic_yaml_never_becomes_blocking_execution_ir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "plain-skill"
            self._write(
                root,
                "SKILL.md",
                "---\n"
                "name: plain-skill\n"
                "description: Plain standards-compatible Skill.\n"
                "---\n"
                "# Plain Skill\n"
                "Use supporting references only when they are relevant.\n",
            )
            self._write(
                root,
                "references/schema.yaml",
                "output_contract:\n"
                "  declared_artifacts: [must-not-exist.md]\n"
                "aggregation:\n"
                "  steps: []\n",
            )

            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        self.assertNotIn("error", loaded)
        self.assertIsNone(loaded.get("execution_contract"))
        workflow = loaded.get("workflow_contract") or {}
        self.assertNotIn("output_contract", workflow)
        self.assertNotIn("aggregation", workflow)

    def test_exact_skill_reference_keeps_legacy_extension_compatible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "legacy-extension"
            self._write(
                root,
                "SKILL.md",
                "---\n"
                "name: legacy-extension\n"
                "description: Explicitly referenced legacy workflow.\n"
                "---\n"
                "# Export\n"
                "Follow `workflows/export.yaml` exactly.\n",
            )
            self._write(
                root,
                "workflows/export.yaml",
                "workers:\n"
                "  renderer:\n"
                "    role: Render the requested payload.\n"
                "output_contract:\n"
                "  declared_artifacts: [result.json]\n"
                "  declared_file_count: 1\n",
            )

            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        execution = loaded.get("execution_contract") or {}
        self.assertTrue((execution.get("diagnostics") or {}).get("valid"))
        self.assertEqual(["renderer"], execution.get("worker_ids"))
        self.assertEqual(
            ["result.json"],
            (execution.get("output_contract") or {}).get("declared_artifacts"),
        )

    def test_external_source_extraction_is_explicit_bounded_and_domain_neutral(self):
        text = (
            "PubMed, FDA, and ChEMBL appear only as ordinary prose and are not claims.\n"
            "Sources: City Open Data; Warehouse WMS，Regional Archive\n"
            "See [Municipal Catalog](https://catalog.example.org/v2/items).\n"
            "Health: https://status.vendor.example/ready.\n"
            "Ignore http://legacy.example.test and ftp://files.example.test.\n"
        )

        self.assertEqual(
            [
                "City Open Data",
                "Warehouse WMS",
                "Regional Archive",
                "Municipal Catalog",
                "status.vendor.example",
            ],
            _extract_external_sources(text),
        )
        self.assertEqual([], _extract_external_sources("PubMed FDA ChEMBL"))
        self.assertEqual(
            [],
            _extract_external_sources(
                ("ordinary context\n" * 40_000)
                + "Sources: This label is beyond the bounded scan\n"
            ),
        )

    def test_no_yaml_loader_uses_only_a_restricted_scalar_fallback(self):
        scalar = (
            "---\n"
            "name: fallback-skill\n"
            "description: Flat metadata only\n"
            "---\n"
            "# Skill\n"
        )
        nested = (
            "---\n"
            "name: nested-skill\n"
            "metadata:\n"
            "  owner: archive-team\n"
            "---\n"
            "# Skill\n"
        )
        with patch("skills.loader._yaml_load_fn", False):
            frontmatter, _ = parse_frontmatter(scalar, strict=True)
            self.assertEqual("fallback-skill", frontmatter["name"])
            with self.assertRaises(FrontmatterParseError) as raised:
                parse_frontmatter(nested, strict=True)
        self.assertEqual(
            "frontmatter_yaml_loader_unavailable",
            raised.exception.code,
        )

    def test_yaml_depth_node_scalar_and_source_bounds_are_diagnostic(self):
        cases = (
            (
                "MAX_COMPILER_STRUCTURE_DEPTH",
                4,
                "orchestrator_id: deep\na:\n  b:\n    c:\n      d:\n        e: value\n",
                "compiler_structure_depth_limit_exceeded",
            ),
            (
                "MAX_COMPILER_STRUCTURE_NODES",
                8,
                "orchestrator_id: nodes\nitems: [a, b, c, d, e, f, g, h, i, j]\n",
                "compiler_structure_node_limit_exceeded",
            ),
            (
                "MAX_COMPILER_SCALAR_CHARS",
                20,
                "orchestrator_id: scalar\ndescription: this text deliberately exceeds the patched aggregate bound\n",
                "compiler_scalar_chars_limit_exceeded",
            ),
            (
                "MAX_COMPILER_YAML_SOURCE_CHARS",
                60,
                "orchestrator_id: source\ndescription: this document deliberately exceeds the patched source bound\n",
                "compiler_yaml_source_limit_exceeded",
            ),
        )
        for constant, limit, yaml_text, expected_code in cases:
            with self.subTest(constant=constant):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    self._base_skill(root)
                    self._write(root, "orchestration/orchestrator.yaml", yaml_text)
                    with patch(f"skills.loader.{constant}", limit):
                        loaded = load_skill_content(
                            root / "SKILL.md", skill_dir=str(root)
                        )
                self.assertIn(expected_code, self._error_codes(loaded))

    def test_resource_discovery_is_complete_and_ui_sample_is_explicitly_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_skill(root)
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                "orchestrator_id: resource-closure-fixture\n",
            )
            for index in range(96):
                self._write(
                    root,
                    f"custom-domain/resource_{index:03d}.md",
                    (
                        "Final output: late_report.md\n"
                        if index == 95 else "evidence\n"
                    ),
                )

            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        linked = (loaded.get("linked_files") or {}).get("custom-domain") or []
        category = (
            ((loaded.get("resource_graph") or {}).get("categories") or {})
            .get("custom-domain")
            or {}
        )
        workflow = loaded.get("workflow_contract") or {}
        diagnostics = (loaded.get("execution_contract") or {}).get("diagnostics") or {}
        self.assertEqual(96, len(linked))
        self.assertEqual(96, category.get("count"))
        self.assertEqual(12, len(category.get("sample") or []))
        self.assertTrue(category.get("sample_truncated"))
        self.assertFalse(workflow.get("scanned_files_truncated", False))
        self.assertEqual(96, workflow.get("scanned_files_total"))
        self.assertEqual(1, workflow.get("semantic_scanned_total_including_skill"))
        self.assertNotIn("late_report.md", workflow.get("artifact_patterns") or [])
        self.assertTrue(diagnostics.get("valid"), diagnostics)
        self.assertNotIn(
            "compiler_field_item_limit_exceeded",
            self._error_codes(loaded),
        )

    def test_supporting_semantic_file_count_remains_advisory_and_not_inferred(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_skill(root)
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                "orchestrator_id: semantic-overflow\n",
            )
            for index in range(4):
                self._write(
                    root,
                    f"references/resource_{index}.md",
                    f"declared output late_{index}_report.md\n",
                )
            with patch("skills.loader.MAX_WORKFLOW_SEMANTIC_SCAN_FILES", 3):
                loaded = load_skill_content(
                    root / "SKILL.md",
                    skill_dir=str(root),
                )

        workflow = loaded.get("workflow_contract") or {}
        diagnostics = loaded.get("package_diagnostics") or {}
        self.assertFalse(workflow.get("scanned_files_truncated", False))
        self.assertTrue(diagnostics.get("valid"))
        self.assertNotIn(
            "workflow_semantic_supporting_file_limit_exceeded",
            self._warning_codes(loaded),
        )
        self.assertNotIn("late_3_report.md", workflow.get("artifact_patterns") or [])

    def test_oversized_reference_is_resource_only_and_does_not_infer_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_skill(root)
            self._write(
                root,
                "references/large.md",
                "Output files:\n- must_not_be_inferred.md\n" + "x" * 500,
            )
            self._write(
                root,
                "references/second.md",
                "Final output: also_not_inferred.json\n" + "y" * 500,
            )
            with patch("skills.loader.MAX_WORKFLOW_SEMANTIC_FILE_CHARS", 64):
                loaded = load_skill_content(
                    root / "SKILL.md",
                    skill_dir=str(root),
                )

        workflow = loaded.get("workflow_contract") or {}
        self.assertIn("references/large.md", (loaded.get("linked_files") or {}).get("references", []))
        self.assertNotIn("workflow_semantic_file_size_exceeded", self._error_codes(loaded))
        self.assertNotIn(
            "workflow_semantic_supporting_file_size_exceeded",
            self._warning_codes(loaded),
        )
        self.assertNotIn("must_not_be_inferred.md", workflow.get("artifact_patterns") or [])
        self.assertNotIn("also_not_inferred.json", workflow.get("artifact_patterns") or [])
        self.assertTrue((loaded.get("package_diagnostics") or {}).get("valid"))

    def test_oversized_authoritative_prose_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: bounded-skill\ndescription: bounded fixture\n---\n"
                "# Skill\n\nLoad [the worker declaration](workers/transform.md).\n",
            )
            self._write(
                root,
                "workers/transform.md",
                "# Worker\nOutput files:\n- result.json\n" + "x" * 500,
            )
            with patch("skills.loader.MAX_WORKFLOW_SEMANTIC_FILE_CHARS", 64):
                loaded = load_skill_content(
                    root / "SKILL.md",
                    skill_dir=str(root),
                )

        self.assertIn(
            "workflow_semantic_file_size_exceeded",
            self._error_codes(loaded),
        )
        self.assertFalse((loaded.get("package_diagnostics") or {}).get("valid"))

    def test_oversized_structured_yaml_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_skill(root)
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                "orchestrator_id: bounded\nrouting_rules:\n  default:\n"
                "    patterns: ['x']\n" + "# padding\n" * 100,
            )
            with patch("skills.loader.MAX_COMPILER_YAML_SOURCE_CHARS", 64):
                loaded = load_skill_content(
                    root / "SKILL.md",
                    skill_dir=str(root),
                )

        self.assertIn(
            "compiler_yaml_source_limit_exceeded",
            self._error_codes(loaded),
        )

    def test_structured_route_artifacts_preserve_unicode_and_nested_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_skill(root)
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                "orchestrator_id: unicode-output\n"
                "routing_rules:\n"
                "  full_report:\n"
                "    patterns: ['完整报告']\n"
                "    deliverable:\n"
                "      artifacts:\n"
                "        - path: 结果/临床总结.md\n"
                "          format: markdown\n"
                "        - filepath: data/指标.json\n"
                "          type: json\n",
            )

            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        execution = loaded.get("execution_contract") or {}
        route = (execution.get("routes") or [])[0]
        contract = route.get("output_contract") or {}
        self.assertEqual(
            ["结果/临床总结.md", "data/指标.json"],
            contract.get("declared_artifacts"),
        )
        self.assertEqual(
            {"结果/临床总结.md": "markdown", "data/指标.json": "json"},
            contract.get("artifact_formats"),
        )


if __name__ == "__main__":
    unittest.main()
