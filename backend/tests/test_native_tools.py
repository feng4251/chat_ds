import unittest

from native_tools import (
    canonicalize_scheduled_tools,
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
        self.assertIn("market_quote", DEFAULT_NATIVE_TOOL_SET)
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

    def test_scheduled_tool_compiler_binds_visible_aliases_to_authority(self):
        self.assertEqual(
            canonicalize_scheduled_tools(
                [
                    "Bash",
                    "mcp__chatds-market-data__market_quote",
                    "market_quote",
                ],
                allowed_tools=frozenset({"market_quote", "cronjob"}),
            ),
            ("market_quote",),
        )
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            canonicalize_scheduled_tools(
                ["web_search"],
                allowed_tools=frozenset({"market_quote"}),
            )
        with self.assertRaisesRegex(ValueError, "unknown"):
            canonicalize_scheduled_tools(
                ["mcp__foreign__market_quote"],
                allowed_tools=frozenset({"market_quote"}),
            )

    def test_scheduled_tool_defaults_do_not_widen_explicit_empty_set(self):
        allowed = frozenset({"market_quote", "web_search", "cronjob"})
        self.assertEqual(
            canonicalize_scheduled_tools([], allowed_tools=allowed),
            (),
        )
        self.assertEqual(
            canonicalize_scheduled_tools(None, allowed_tools=allowed),
            ("web_search", "market_quote"),
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
