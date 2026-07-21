import unittest

from prompt.builder import SESSION_SKILL_USAGE_GUIDANCE
from skills.manager import (
    _build_execution_plan_hint,
    _build_manifest_guidance,
)


class GenericSkillPromptGuidanceTests(unittest.TestCase):
    def test_resource_only_skill_does_not_manufacture_execution_shape(self):
        steps, hint = _build_manifest_guidance(
            linked_files={
                "references": ["references/domain.md"],
                # A filename/category is not a compiled worker declaration.
                "workers": ["workers/example.md"],
                "assets": ["assets/template.bin"],
            },
            resource_graph={
                "categories": {
                    "references": {"count": 1},
                    "workers": {"count": 1},
                    "assets": {"count": 1},
                }
            },
            workflow_contract={},
            execution_contract={},
            output_contract={},
        )

        guidance = " ".join([*steps, hint])
        self.assertIn("request-relevant bundled resources", guidance)
        self.assertIn("No compiled orchestration", hint)
        self.assertNotIn("delegate_task", guidance)
        self.assertNotIn("merge_files", guidance)
        self.assertNotIn("artifact set in the session workspace", guidance)
        self.assertIn("skill_copy_resource", guidance)

    def test_compiled_contract_enables_only_its_declared_shapes(self):
        output = {
            "declared_modular_files": ["part-a.json", "part-b.json"],
            "declared_file_count": 3,
            "declared_final_artifact": "bundle.json",
            "merge_input_order": ["part-a.json", "part-b.json"],
            "merge_command": "combine part-a.json part-b.json > bundle.json",
            "merge_mandatory": True,
            "post_merge_checks": ["bundle parses"],
        }
        steps, hint = _build_manifest_guidance(
            linked_files={"workflows": ["workflows/pipeline.yaml"]},
            resource_graph={"categories": {"workflows": {"count": 1}}},
            workflow_contract={
                "workflow_files": ["workflows/pipeline.yaml"],
                "artifact_patterns": ["part-*.json", "bundle.json"],
                "requires_modular_artifacts": True,
                "requires_merge": True,
                "sanity_checks": ["all records accounted for"],
            },
            execution_contract={
                "routes": [{"id": "normalize", "workers": ["clean"]}],
                "workers": [{"id": "clean"}],
                "output_contract": output,
            },
            output_contract=output,
        )

        guidance = " ".join([*steps, hint])
        self.assertIn("exactly one declared", guidance)
        self.assertIn("delegate_task", guidance)
        self.assertIn("exactly the declared artifact set", guidance)
        self.assertIn("merge_files", guidance)
        self.assertIn("validation and completion checks", guidance)
        self.assertIn("worker execution", hint)
        self.assertIn("artifact-set production", hint)

        plan = _build_execution_plan_hint(output)
        self.assertIn("2 declared modular artifacts", plan)
        self.assertIn("merge_files", plan)
        self.assertIn("bundle.json", plan)

    def test_single_artifact_contract_stays_single_artifact(self):
        output = {"declared_final_artifact": "answer.yaml"}
        steps, hint = _build_manifest_guidance(
            linked_files={},
            resource_graph={},
            workflow_contract={},
            execution_contract={"output_contract": output},
            output_contract=output,
        )

        self.assertEqual([], steps)
        self.assertIn("No compiled orchestration", hint)
        plan = _build_execution_plan_hint(output)
        self.assertIn("answer.yaml", plan)
        self.assertNotIn("modular", plan.casefold())
        self.assertNotIn("merge_files", plan)
        self.assertNotIn("report", plan.casefold())

    def test_stable_prompt_makes_orchestration_strictly_conditional(self):
        guidance = SESSION_SKILL_USAGE_GUIDANCE
        self.assertIn("A Skill becomes active", guidance)
        self.assertIn("only when the compiled", guidance)
        self.assertIn("only when the output contract or explicit request declares", guidance)
        self.assertIn("Never infer multi-agent work", guidance)
        self.assertIn("instruction-only", guidance)
        self.assertNotIn("For any broad, multi-discipline deliverable", guidance)


if __name__ == "__main__":
    unittest.main()
