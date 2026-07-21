import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import (
    HarnessRunState,
    _artifact_synthesis_output_paths,
    _compiled_artifact_paths_for_skill,
    _compile_run_artifact_plan,
    _has_merged_artifact_for_contract,
    _workflow_gate_call_error,
    _workflow_gate_tool_policy,
)


def _contract() -> dict:
    output = {
        "declared_modular_files": ["01.md"],
        "declared_final_artifact": "{PROJECT}_PACKAGE.md",
        "merge_mandatory": True,
        "merge_input_order": ["01.md"],
        "merge_separator": "\n",
    }
    return {
        "execution_contract": {
            "workers": [],
            "routes": [],
            "output_contract": output,
        },
        "output_contract": output,
        "requires_modular_artifacts": True,
        "requires_merge": True,
    }


class ArtifactPlanRuntimeTests(unittest.TestCase):
    @staticmethod
    def _ready_state(contract: dict, user_text: str) -> HarnessRunState:
        state = HarnessRunState(
            available_tools={"delegate_task"},
            original_user_text=user_text,
        )
        state.session_skill_names.add("generic")
        state.viewed_skill_names.add("generic")
        state.viewed_skill_files["generic"] = {"__manifest__"}
        state.viewed_skill_categories["generic"] = {"formats"}
        state.skill_available_categories["generic"] = {"formats"}
        state.skill_workflow_contracts["generic"] = contract
        state.skill_execution_plans["generic"] = {
            "selection": "full",
            "requires_full_output": True,
            "required_workers": [],
            "bootstrap_sources": [],
            "aggregation_steps": [],
        }
        state.skill_artifact_plans["generic"], bindings = (
            _compile_run_artifact_plan(contract, user_text)
        )
        if bindings:
            state.skill_artifact_bindings["generic"] = bindings
        return state

    def test_only_exact_line_level_user_binding_bypasses_binding_agent(self):
        unresolved, bindings = _compile_run_artifact_plan(
            _contract(),
            "Please build this. The PROJECT concept is alpha.",
        )
        self.assertFalse(unresolved["valid"])
        self.assertEqual({}, bindings)

        resolved, bindings = _compile_run_artifact_plan(
            _contract(),
            "Please build this.\nPROJECT=alpha\n",
        )
        self.assertTrue(resolved["valid"])
        self.assertEqual({"PROJECT": "alpha"}, bindings)
        self.assertEqual("alpha_PACKAGE.md", resolved["merge"]["output_path"])

    def test_typed_binding_result_compiles_and_is_recorded(self):
        state = HarnessRunState(original_user_text="Build a package for Alpha")
        state.skill_workflow_contracts["generic"] = _contract()
        state.skill_execution_plans["generic"] = {
            "requires_full_output": True,
            "required_workers": [],
            "bootstrap_sources": [],
            "aggregation_steps": [],
        }
        draft, _ = _compile_run_artifact_plan(
            _contract(), state.original_user_text
        )
        state.skill_artifact_plans["generic"] = draft
        args = {
            "goal": "resolve naming",
            "skill_name": "generic",
            "step_type": "artifact_binding",
            "step_id": "artifact-bindings",
            "workflow_stage": "artifact-binding",
            "tools": ["skill_view"],
        }
        result = {
            "results": [{
                "index": 0,
                "status": "completed",
                "skill_name": "generic",
                "step_type": "artifact_binding",
                "step_id": "artifact-bindings",
                "result_path": "results/binding.txt",
                "result_chars": 500,
                "intent_selections": {"PROJECT": "alpha"},
            }],
        }

        update = state.record_delegate_task(args, result)

        self.assertEqual(["artifact-bindings"], update["completed_step_ids"])
        self.assertIn("generic", state.skill_completed_artifact_binding)
        self.assertEqual(
            "alpha_PACKAGE.md",
            state.skill_artifact_plans["generic"]["merge"]["output_path"],
        )
        self.assertEqual(
            {"PROJECT": "alpha"}, state.skill_artifact_bindings["generic"]
        )

    def test_binding_must_return_exact_keys_and_safe_values(self):
        state = HarnessRunState(original_user_text="Build a package")
        state.skill_workflow_contracts["generic"] = _contract()
        state.skill_execution_plans["generic"] = {
            "requires_full_output": True,
            "required_workers": [],
            "bootstrap_sources": [],
            "aggregation_steps": [],
        }
        state.skill_artifact_plans["generic"], _ = _compile_run_artifact_plan(
            _contract(), state.original_user_text
        )
        args = {
            "goal": "resolve naming",
            "skill_name": "generic",
            "step_type": "artifact_binding",
            "step_id": "artifact-bindings",
            "tools": ["skill_view"],
        }
        result = {
            "results": [{
                "status": "completed",
                "skill_name": "generic",
                "step_type": "artifact_binding",
                "step_id": "artifact-bindings",
                "result_path": "results/binding.txt",
                "result_chars": 500,
                "intent_selections": {"PROJECT": "../escape", "EXTRA": "x"},
            }],
        }

        update = state.record_delegate_task(args, result)

        self.assertEqual(["artifact-bindings"], update["retryable_failed_step_ids"])
        self.assertNotIn("generic", state.skill_completed_artifact_binding)
        self.assertIn("every and only", state.skill_failed_artifact_binding["generic"])

    def test_unresolved_plan_precedes_missing_modular_artifacts(self):
        state = self._ready_state(_contract(), "Build the Alpha package")

        needed, reason = state.needs_more_skill_workflow()

        self.assertTrue(needed)
        self.assertIn("delegate artifact binding", reason)
        self.assertNotIn("modular", reason)

    def test_completed_binding_closes_plan_before_modular_synthesis(self):
        state = self._ready_state(_contract(), "Build the Alpha package")
        state.record_delegate_task(
            {
                "skill_name": "generic",
                "step_type": "artifact_binding",
                "step_id": "artifact-bindings",
                "workflow_stage": "artifact-binding",
                "tools": [],
            },
            {
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "step_type": "artifact_binding",
                    "step_id": "artifact-bindings",
                    "result_path": "results/binding.txt",
                    "result_chars": 500,
                    "intent_selections": {"PROJECT": "alpha"},
                }],
            },
        )

        needed, reason = state.needs_more_skill_workflow()

        self.assertTrue(needed)
        self.assertIn("modular/checklist", reason)
        self.assertTrue(state.skill_artifact_plans["generic"]["valid"])

    def test_valid_plan_goes_directly_to_modular_synthesis(self):
        state = self._ready_state(
            _contract(),
            "Build the package.\nPROJECT=alpha\n",
        )

        needed, reason = state.needs_more_skill_workflow()

        self.assertTrue(needed)
        self.assertIn("modular/checklist", reason)
        self.assertNotIn("artifact binding", reason)

    def test_non_binding_plan_error_fails_closed_before_synthesis(self):
        contract = _contract()
        contract["output_contract"]["declared_modular_files"] = [
            "01.md", "01.md",
        ]
        contract["execution_contract"]["output_contract"] = contract[
            "output_contract"
        ]
        state = self._ready_state(
            contract,
            "Build the package.\nPROJECT=alpha\n",
        )

        needed, reason = state.needs_more_skill_workflow()
        policy = _workflow_gate_tool_policy(reason, state.available_tools)

        self.assertTrue(needed)
        self.assertIn("invalid artifact contract", reason)
        self.assertNotIn("modular", reason)
        self.assertEqual([], policy["tools"])
        self.assertEqual(0, policy["max_calls"])

    def test_synthesis_paths_are_concrete_after_binding(self):
        plan, _ = _compile_run_artifact_plan(
            _contract(),
            "Build the package.\nPROJECT=alpha\n",
        )

        outputs, final_output = _artifact_synthesis_output_paths(
            plan,
            _contract()["output_contract"],
        )

        self.assertEqual(["01.md"], outputs)
        self.assertEqual("alpha_PACKAGE.md", final_output)
        self.assertNotIn("{PROJECT}", "\n".join([*outputs, final_output]))

    def test_semantic_final_is_part_of_synthesis_and_readiness(self):
        output = {
            "declared_modular_files": ["01.md"],
            "declared_final_artifact": "site/index.html",
            "merge_mandatory": False,
        }
        contract = {
            "execution_contract": {
                "workers": [],
                "routes": [],
                "output_contract": output,
            },
            "output_contract": output,
            "requires_modular_artifacts": True,
        }
        state = self._ready_state(contract, "Build the semantic package")
        plan = state.skill_artifact_plans["generic"]

        outputs, final_output = _artifact_synthesis_output_paths(plan, output)

        self.assertEqual(["01.md", "site/index.html"], outputs)
        self.assertEqual("site/index.html", final_output)
        self.assertEqual(
            ["01.md", "site/index.html"],
            _compiled_artifact_paths_for_skill(state, "generic"),
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "agent_loop.get_workspace",
            return_value=Path(temp_dir),
        ):
            root = Path(temp_dir)
            (root / "01.md").write_text("# Module\n", encoding="utf-8")
            state.artifacts = [{"path": "01.md", "size_bytes": 9}]
            needed, reason = state.needs_more_skill_workflow()
            self.assertTrue(needed)
            self.assertIn("artifacts", reason)
            final = root / "site" / "index.html"
            final.parent.mkdir()
            final.write_text("<main>Complete</main>", encoding="utf-8")
            state.artifacts.append({
                "path": "site/index.html",
                "size_bytes": final.stat().st_size,
            })
            needed, reason = state.needs_more_skill_workflow()
            self.assertFalse(needed, reason)

    def test_non_markdown_merge_receipt_satisfies_merge_gate(self):
        contract = {
            "requires_merge": True,
            "output_contract": {
                "declared_modular_files": ["01.txt", "02.txt"],
                "declared_final_artifact": "FINAL.txt",
                "merge_mandatory": True,
            },
        }
        state = HarnessRunState()
        state.artifacts = [{
            "path": "FINAL.txt",
            "size_bytes": 20,
            "source_tool": "merge_files",
            "is_merged": True,
            "input_files": ["01.txt", "02.txt"],
        }]

        self.assertTrue(_has_merged_artifact_for_contract(state, contract))

    def test_missing_output_contract_keeps_legacy_modular_gate(self):
        contract = {
            "worker_files": ["workers/noop.yaml"],
            "requires_modular_artifacts": True,
        }
        state = self._ready_state(contract, "Build a package")

        needed, reason = state.needs_more_skill_workflow()

        self.assertTrue(needed)
        self.assertIn("modular/checklist", reason)

    def test_compiled_merge_arguments_are_immutable_at_gate(self):
        policy = _workflow_gate_tool_policy(
            "create the declared merged final report artifact for session skill 'generic'",
            {"merge_files"},
        )
        exact = {
            "output_filepath": "alpha_PACKAGE.md",
            "input_files": ["01.md"],
            "separator": "\n",
        }
        policy["expected_merge_args"] = dict(exact)
        self.assertEqual(
            "",
            _workflow_gate_call_error(
                policy, "merge_files", exact, prior_call_count=0
            ),
        )
        changed = {**exact, "output_filepath": "other.md"}
        self.assertIn(
            "exact compiled ArtifactPlan arguments",
            _workflow_gate_call_error(
                policy, "merge_files", changed, prior_call_count=0
            ),
        )


if __name__ == "__main__":
    unittest.main()
