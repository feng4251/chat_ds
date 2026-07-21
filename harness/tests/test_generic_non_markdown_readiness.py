import unittest

from agent_loop import HarnessRunState, _has_modular_artifacts_for_contract


class GenericNonMarkdownReadinessTests(unittest.TestCase):
    def test_structured_json_and_csv_outputs_are_format_agnostic(self):
        state = HarnessRunState(user_id="u", session_id="s")
        state.artifacts = [
            {"path": "results/summary.json", "size_bytes": 12},
            {"path": "results/records.csv", "size_bytes": 18},
        ]
        contract = {
            "output_contract": {
                "declared_artifacts": [
                    "results/summary.json",
                    "results/records.csv",
                ],
            },
        }

        self.assertTrue(_has_modular_artifacts_for_contract(state, contract))

    def test_prose_discovered_non_markdown_pattern_is_honored(self):
        state = HarnessRunState(user_id="u", session_id="s")
        state.artifacts = [
            {"path": "catalog.json", "size_bytes": 12},
        ]

        self.assertTrue(
            _has_modular_artifacts_for_contract(
                state,
                {"artifact_patterns": ["catalog.json"]},
            )
        )

    def test_missing_declared_non_markdown_output_is_not_ready(self):
        state = HarnessRunState(user_id="u", session_id="s")
        state.artifacts = [
            {"path": "results/summary.json", "size_bytes": 12},
        ]
        contract = {
            "output_contract": {
                "declared_artifacts": [
                    "results/summary.json",
                    "results/records.csv",
                ],
            },
        }

        self.assertFalse(_has_modular_artifacts_for_contract(state, contract))


if __name__ == "__main__":
    unittest.main()
