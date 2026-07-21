import unittest

from model_routing import (
    canonical_agent_model_id,
    filter_agentic_fallback_model_ids,
    is_agentic_auxiliary_only_model,
)


class BackendModelRoutingTests(unittest.TestCase):
    def test_historic_primary_alias_is_canonicalized(self):
        self.assertEqual(
            "deepseek_v4_pro", canonical_agent_model_id("AgentModel")
        )
        self.assertEqual("qwen3_5", canonical_agent_model_id("qwen3_5"))

    def test_qwen_is_auxiliary_only_for_agentic_execution(self):
        self.assertTrue(is_agentic_auxiliary_only_model("qwen3_5"))
        self.assertFalse(is_agentic_auxiliary_only_model("deepseek_v4_pro"))

    def test_agentic_fallback_filter_is_stable_unique_and_auditable(self):
        allowed, removed = filter_agentic_fallback_model_ids(
            [
                "qwen3_5",
                "custom-a",
                "custom-a",
                "deepseek_v4_pro",
                "custom-b",
            ],
            requested_model_id="deepseek_v4_pro",
        )
        self.assertEqual(["custom-a", "custom-b"], allowed)
        self.assertEqual(["qwen3_5", "deepseek_v4_pro"], removed)

    def test_primary_alias_is_not_a_duplicate_fallback(self):
        allowed, removed = filter_agentic_fallback_model_ids(
            ["AgentModel", "custom"],
            requested_model_id="deepseek_v4_pro",
        )
        self.assertEqual(["custom"], allowed)
        self.assertEqual(["deepseek_v4_pro"], removed)


if __name__ == "__main__":
    unittest.main()
