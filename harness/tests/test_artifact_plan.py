import copy
import unittest

from artifact_plan import ArtifactPlanError, compile_artifact_plan


def _three_module_contract() -> dict:
    output = {
        "declared_modular_files": [
            "01_inventory.md",
            "02_analysis.md",
            "03_decision.md",
        ],
        "declared_modular_file_count": 3,
        "declared_ancillary_files": ["README.md", "_checklist.md"],
        "declared_final_artifact": "{PROJECT}_PACKAGE.md",
        "declared_format_final_artifacts": ["FULL_PACKAGE.md"],
        "declared_file_count": 6,
        "merge_mandatory": True,
        "merge_input_order": [
            "01_inventory.md",
            "02_analysis.md",
            "03_decision.md",
        ],
        "merge_separator": "\n\n",
        "artifact_set_policy": {
            "mode": "exact",
            "artifacts": [
                "01_inventory.md",
                "02_analysis.md",
                "03_decision.md",
                "README.md",
                "_checklist.md",
                "FULL_PACKAGE.md",
            ],
        },
        "sections": [
            {
                "id": "inventory",
                "title": "Inventory",
                "order": 1,
                "source_workers": ["collector"],
                "applicability": "When inventory evidence is requested",
                "key_elements": [
                    "Canonical inventory table",
                    {
                        "provenance": ["source", "retrieved_at"],
                        "display_order": "after-provenance",
                    },
                ],
                "source_file": "orchestration/orchestrator.yaml",
            },
            {
                "id": "analysis",
                "title": "Analysis",
                "order": 2,
                "source_workers": ["analyst", "collector"],
            },
            {
                "id": "decision",
                "title": "Decision",
                "order": 3,
                "source_workers": ["reviewer"],
            },
        ],
    }
    return {
        "schema_version": 1,
        "workers": [
            {"id": "collector", "file": "workers/collector.yaml"},
            {"id": "analyst", "file": "workers/analyst.yaml"},
            {"id": "reviewer", "file": "workers/reviewer.yaml"},
        ],
        "worker_ids": ["collector", "analyst", "reviewer"],
        "output_contract": output,
        "quality_contract": {
            "required_module_markers": ["**Owner**", "**Evidence**"],
            "section_file_mapping": [
                {"file": "01_inventory.md", "section_ids": ["inventory"]},
                {"file": "02_analysis.md", "section_ids": ["analysis"]},
                {"file": "03_decision.md", "section_ids": ["decision"]},
            ],
        },
        "diagnostics": {"valid": True, "errors": [], "warnings": []},
    }


class ArtifactPlanCompilerTests(unittest.TestCase):
    def test_final_artifact_does_not_imply_byte_merge(self):
        contract = _three_module_contract()
        contract["output_contract"]["merge_mandatory"] = False

        plan = compile_artifact_plan(
            contract,
            bindings={"project": "alpha"},
        )

        self.assertTrue(plan.valid, plan.errors)
        self.assertEqual("alpha_PACKAGE.md", plan.final_path)
        self.assertFalse(plan.merge.required)
        self.assertFalse(plan.merge.dispatchable)

    def test_legacy_merge_command_without_flag_still_compiles_a_merge(self):
        contract = _three_module_contract()
        contract["output_contract"].pop("merge_mandatory", None)
        contract["output_contract"]["merge_command"] = (
            "cat 01_inventory.md 02_analysis.md 03_decision.md > {PROJECT}_PACKAGE.md"
        )

        plan = compile_artifact_plan(contract, bindings={"project": "alpha"})

        self.assertTrue(plan.valid, plan.errors)
        self.assertTrue(plan.merge.required)
        self.assertTrue(plan.merge.dispatchable)
        self.assertEqual("alpha_PACKAGE.md", plan.merge.output_path)

    def test_explicit_binding_compiles_exact_closed_artifact_plan(self):
        plan = compile_artifact_plan(
            _three_module_contract(),
            bindings={"project": "alpha"},
        )

        self.assertTrue(plan.valid, plan.errors)
        self.assertEqual(
            ("01_inventory.md", "02_analysis.md", "03_decision.md"),
            plan.modular_paths,
        )
        self.assertEqual(("README.md", "_checklist.md"), plan.ancillary_paths)
        self.assertEqual("alpha_PACKAGE.md", plan.final_path)
        self.assertEqual((), plan.unresolved_placeholders)
        self.assertEqual(("**Owner**", "**Evidence**"), plan.required_markers)
        self.assertEqual(
            {
                "inventory": ["collector"],
                "analysis": ["analyst", "collector"],
                "decision": ["reviewer"],
            },
            plan.to_dict()["section_to_source_workers"],
        )
        self.assertEqual(
            ["01_inventory.md", "02_analysis.md", "03_decision.md"],
            plan.to_dict()["merge"]["input_paths"],
        )
        self.assertEqual("\n\n", plan.merge.separator)
        self.assertEqual("alpha_PACKAGE.md", plan.merge.output_path)
        self.assertTrue(plan.merge.dispatchable)
        self.assertIs(plan, plan.require_valid())

    def test_section_metadata_is_lossless_and_unselected_activation_is_unknown(self):
        plan = compile_artifact_plan(
            _three_module_contract(),
            bindings={"project": "alpha"},
        )

        self.assertTrue(plan.valid, plan.errors)
        payload = plan.to_dict()
        inventory = payload["sections"][0]
        self.assertEqual(
            "When inventory evidence is requested",
            inventory["applicability"],
        )
        self.assertEqual(
            [
                "Canonical inventory table",
                {
                    "provenance": ["source", "retrieved_at"],
                    "display_order": "after-provenance",
                },
            ],
            inventory["key_elements"],
        )
        self.assertEqual(
            ["provenance", "display_order"],
            list(inventory["key_elements"][1]),
        )
        self.assertEqual(
            "orchestration/orchestrator.yaml",
            inventory["source_file"],
        )
        self.assertIsNone(payload["selection_context"])
        self.assertEqual(
            {
                "inventory": "unknown",
                "analysis": "unknown",
                "decision": "unknown",
            },
            payload["section_activation"],
        )
        self.assertEqual([], payload["active_section_ids"])
        self.assertEqual([], payload["inactive_section_ids"])
        self.assertTrue(all(
            section["activation_status"] == "unknown"
            and section["selected_source_workers"] is None
            and section["unselected_source_workers"] is None
            for section in payload["sections"]
        ))

        # A caller cannot mutate the frozen plan through a returned nested value.
        inventory["key_elements"][1]["provenance"].append("mutated")
        self.assertEqual(
            ["source", "retrieved_at"],
            plan.sections[0].key_elements[1]["provenance"],
        )

    def test_selected_workers_project_activation_without_dropping_sections(self):
        contract = _three_module_contract()
        contract["output_contract"]["sections"].append({
            "id": "package-summary",
            "title": "Package Summary",
            "order": 4,
            "key_elements": ["Cross-worker synthesis"],
            "source_file": "formats/report.yaml",
        })

        plan = compile_artifact_plan(
            contract,
            bindings={"project": "alpha"},
            selection_context={"selected_workers": ["collector"]},
        )

        self.assertTrue(plan.valid, plan.errors)
        payload = plan.to_dict()
        self.assertEqual(
            {"selected_workers": ["collector"]},
            payload["selection_context"],
        )
        self.assertEqual(4, len(payload["sections"]))
        self.assertEqual(
            {
                "inventory": "active",
                "analysis": "active",
                "decision": "inactive",
                "package-summary": "active",
            },
            payload["section_activation"],
        )
        self.assertEqual(
            ["inventory", "analysis", "package-summary"],
            payload["active_section_ids"],
        )
        self.assertEqual(["decision"], payload["inactive_section_ids"])
        analysis = next(
            section for section in payload["sections"]
            if section["section_id"] == "analysis"
        )
        self.assertEqual(["collector"], analysis["selected_source_workers"])
        self.assertEqual(["analyst"], analysis["unselected_source_workers"])

        empty_projection = compile_artifact_plan(
            contract,
            bindings={"project": "alpha"},
            selection_context={"selected_workers": []},
        ).to_dict()
        self.assertEqual(
            ["package-summary"],
            empty_projection["active_section_ids"],
        )
        self.assertEqual(
            ["inventory", "analysis", "decision"],
            empty_projection["inactive_section_ids"],
        )

    def test_selection_and_section_metadata_fail_closed_when_invalid_or_oversized(self):
        unknown_worker = compile_artifact_plan(
            _three_module_contract(),
            bindings={"project": "alpha"},
            selection_context={"selected_workers": ["not-declared"]},
        )
        self.assertFalse(unknown_worker.valid)
        self.assertIn(
            "unknown_selected_worker",
            {item.code for item in unknown_worker.errors},
        )

        cases = [
            (
                "applicability",
                "x" * 4097,
                "section_metadata_limit_exceeded",
            ),
            (
                "source_file",
                "x" * 1025,
                "section_metadata_limit_exceeded",
            ),
            (
                "key_elements",
                ["x" * 65537],
                "section_key_elements_limit_exceeded",
            ),
        ]
        for field, value, expected_code in cases:
            with self.subTest(field=field):
                contract = _three_module_contract()
                contract["output_contract"]["sections"][0][field] = value
                rejected = compile_artifact_plan(
                    contract,
                    bindings={"project": "alpha"},
                )
                self.assertFalse(rejected.valid)
                self.assertIn(
                    expected_code,
                    {item.code for item in rejected.errors},
                )

    def test_missing_binding_is_explicit_and_fails_closed(self):
        plan = compile_artifact_plan(_three_module_contract())

        self.assertFalse(plan.valid)
        self.assertEqual(("PROJECT",), plan.unresolved_placeholders)
        self.assertIsNone(plan.final_path)
        self.assertEqual("{PROJECT}_PACKAGE.md", plan.final_artifact.template)
        self.assertFalse(plan.merge.dispatchable)
        self.assertIn(
            "unresolved_artifact_placeholder",
            {item.code for item in plan.errors},
        )
        with self.assertRaises(ArtifactPlanError):
            plan.require_valid()

    def test_compilation_is_idempotent_and_merge_plan_is_unique(self):
        contract = _three_module_contract()
        first = compile_artifact_plan(contract, bindings={"PROJECT": "alpha"})
        second = compile_artifact_plan(contract, bindings={"project": "alpha"})

        self.assertEqual(first, second)
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(3, len(first.merge.input_paths))
        self.assertEqual(3, len(set(first.merge.input_paths)))
        self.assertEqual(set(first.modular_paths), set(first.merge.input_paths))
        self.assertEqual("alpha_PACKAGE.md", first.merge.output_path)

        duplicate = copy.deepcopy(contract)
        duplicate["output_contract"]["merge_input_order"] = [
            "01_inventory.md",
            "01_inventory.md",
            "03_decision.md",
        ]
        rejected = compile_artifact_plan(
            duplicate,
            bindings={"project": "alpha"},
        )
        self.assertFalse(rejected.valid)
        self.assertFalse(rejected.merge.dispatchable)
        self.assertIn(
            "duplicate_merge_input",
            {item.code for item in rejected.errors},
        )

    def test_literal_placeholder_duplicate_resolution_and_unsafe_paths_reject(self):
        cases = []

        literal = _three_module_contract()
        literal["output_contract"]["declared_final_artifact"] = "TODO.md"
        literal["output_contract"].pop("artifact_set_policy")
        cases.append((literal, {"project": "alpha"}, "literal_placeholder_path"))

        unsafe = _three_module_contract()
        unsafe["output_contract"]["declared_modular_files"][0] = "../escape.md"
        unsafe["output_contract"]["merge_input_order"][0] = "../escape.md"
        unsafe["output_contract"].pop("artifact_set_policy")
        cases.append((unsafe, {"project": "alpha"}, "unsafe_artifact_path"))

        duplicate = _three_module_contract()
        duplicate["output_contract"]["declared_modular_files"] = [
            "{PROJECT}.md",
            "alpha.md",
            "03_decision.md",
        ]
        duplicate["output_contract"]["merge_input_order"] = [
            "{PROJECT}.md",
            "alpha.md",
            "03_decision.md",
        ]
        duplicate["output_contract"].pop("artifact_set_policy")
        cases.append(
            (duplicate, {"project": "alpha"}, "duplicate_resolved_artifact_path")
        )

        for contract, bindings, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                plan = compile_artifact_plan(contract, bindings=bindings)
                self.assertFalse(plan.valid)
                self.assertFalse(plan.merge.dispatchable)
                self.assertIn(expected_code, {item.code for item in plan.errors})


if __name__ == "__main__":
    unittest.main()
