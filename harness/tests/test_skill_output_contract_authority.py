import unittest

from agent_loop import HarnessRunState, _needs_complex_artifact_gate


class SkillOutputContractAuthorityTests(unittest.TestCase):
    def _state(self) -> HarnessRunState:
        state = HarnessRunState(
            skill_workflow_activation="complex_deliverable",
            original_user_text="Create the complete declared package.",
        )
        state.skill_execution_plans["small-package"] = {
            "requires_full_output": True,
        }
        return state

    def test_structured_skill_contract_overrides_legacy_120k_gate(self):
        state = self._state()
        state.skill_workflow_contracts["small-package"] = {
            "output_contract": {
                "declared_artifacts": ["REPORT.md"],
                "declared_final_artifact": "REPORT.md",
                "expected_min_bytes": 1_024,
            },
        }
        state.successful_write_sizes.append(2_048)
        state.successful_search_count = 1

        self.assertFalse(
            _needs_complex_artifact_gate(
                state,
                state.original_user_text,
                "The declared report is complete.",
            )
        )

    def test_legacy_complex_request_without_contract_keeps_density_gate(self):
        state = self._state()
        state.successful_write_sizes.append(2_048)
        state.successful_search_count = 1

        self.assertTrue(
            _needs_complex_artifact_gate(
                state,
                state.original_user_text,
                "A short report draft.",
            )
        )

    def test_instruction_only_skill_does_not_invent_legacy_report_size(self):
        state = HarnessRunState(
            skill_workflow_activation="explicit_skill_request",
            original_user_text=(
                "Run portable-note and create a concise one-page Markdown report."
            ),
        )
        state.session_skill_names.add("portable-note")
        state.viewed_skill_names.add("portable-note")
        state.skill_workflow_contracts["portable-note"] = {
            "execution_contract": {
                "environment_contract": {"allowed_tools": ["write_file"]},
            },
        }
        state.successful_write_sizes.append(4_096)
        state.successful_search_count = 1

        self.assertFalse(
            _needs_complex_artifact_gate(
                state,
                state.original_user_text,
                "The requested one-page artifact is complete.",
            )
        )

    def test_instruction_only_skill_still_blocks_integrity_failures(self):
        state = HarnessRunState(
            skill_workflow_activation="explicit_skill_request",
            original_user_text=(
                "Run portable-note and create a concise one-page Markdown report."
            ),
        )
        state.session_skill_names.add("portable-note")
        state.skill_workflow_contracts["portable-note"] = {
            "execution_contract": {},
        }
        state.successful_write_sizes.append(4_096)
        state.last_successful_artifact_at = 2

        state.invalid_placeholder_last_at = 3
        self.assertTrue(
            _needs_complex_artifact_gate(
                state, state.original_user_text, "Done."
            )
        )

        state.invalid_placeholder_last_at = 0
        state.tool_error_count = 2
        state.last_tool_error_at = 3
        self.assertTrue(
            _needs_complex_artifact_gate(
                state, state.original_user_text, "Done."
            )
        )


if __name__ == "__main__":
    unittest.main()
