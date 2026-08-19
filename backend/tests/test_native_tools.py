import unittest

from native_tools import (
    canonicalize_scheduled_tools,
    DEFAULT_NATIVE_TOOL_SET,
    DEFAULT_NATIVE_TOOLS,
    deepseek_harness_native_tool_groups,
    deepseek_harness_native_tools,
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

    def test_deepseek_native_tool_groups_compile_from_chatds_capabilities(self):
        self.assertEqual(
            deepseek_harness_native_tool_groups([
                "web_search",
                "execute_code",
                "read_file",
                "delegate_task",
                "market_quote",
            ]),
            ("web", "shell", "files", "subagents"),
        )
        self.assertEqual(
            deepseek_harness_native_tools([
                "web_search",
                "execute_code",
                "read_file",
                "delegate_task",
            ]),
            (
                "web_search",
                "bash",
                "job_output",
                "job_list",
                "job_kill",
                "read",
                "write",
                "edit",
                "glob",
                "grep",
                "subagent",
                "send_message",
                "interrupt_agent",
                "list_agents",
            ),
        )
        with self.assertRaisesRegex(ValueError, "unknown"):
            deepseek_harness_native_tool_groups(["nonexistent_capability"])


if __name__ == "__main__":
    unittest.main()
