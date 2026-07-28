import unittest

from native_tools import (
    DEFAULT_NATIVE_TOOL_SET,
    DEFAULT_NATIVE_TOOLS,
    UNATTENDED_DEFAULT_NATIVE_TOOLS,
)


class NativeToolCatalogTests(unittest.TestCase):
    def test_default_catalog_is_unique_and_includes_atomic_merge(self):
        self.assertEqual(len(DEFAULT_NATIVE_TOOLS), len(DEFAULT_NATIVE_TOOL_SET))
        self.assertIn("merge_files", DEFAULT_NATIVE_TOOL_SET)
        self.assertIn("run_skill_script", DEFAULT_NATIVE_TOOL_SET)
        self.assertIn("run_declared_command", DEFAULT_NATIVE_TOOL_SET)
        self.assertIn("skill_http_post_json", DEFAULT_NATIVE_TOOL_SET)
        self.assertIn("skill_copy_resource", DEFAULT_NATIVE_TOOL_SET)
        self.assertIn(
            "submit_knowledge_gate_decisions",
            DEFAULT_NATIVE_TOOL_SET,
        )

    def test_unattended_catalog_is_a_safe_ordered_subset(self):
        self.assertTrue(set(UNATTENDED_DEFAULT_NATIVE_TOOLS) < DEFAULT_NATIVE_TOOL_SET)
        self.assertFalse(
            {"cronjob", "clarify", "delegate_task"}
            & set(UNATTENDED_DEFAULT_NATIVE_TOOLS)
        )
        self.assertEqual(
            list(UNATTENDED_DEFAULT_NATIVE_TOOLS),
            [
                name for name in DEFAULT_NATIVE_TOOLS
                if name not in {"cronjob", "clarify", "delegate_task"}
            ],
        )


if __name__ == "__main__":
    unittest.main()
