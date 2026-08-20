import unittest

from model_routing import (
    DEFAULT_AGENT_MODEL_ID,
    canonical_agent_model_id,
)


class BackendModelRoutingTests(unittest.TestCase):
    def test_new_default_does_not_rebind_historic_agent_model_alias(self):
        self.assertEqual("deepseek_v4_pro", DEFAULT_AGENT_MODEL_ID)
        self.assertEqual(
            "deepseek_v4_pro", canonical_agent_model_id("AgentModel")
        )

    def test_historic_primary_alias_is_canonicalized(self):
        self.assertEqual(
            "deepseek_v4_pro", canonical_agent_model_id("AgentModel")
        )
        self.assertEqual("qwen3_5", canonical_agent_model_id("qwen3_5"))

if __name__ == "__main__":
    unittest.main()
