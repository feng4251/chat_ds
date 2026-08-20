import unittest

from native_tools import (
    canonicalize_scheduled_platform_capabilities,
    DEEPSEEK_HARNESS_NATIVE_TOOLS,
    deepseek_harness_native_tools,
    PLATFORM_IO_CAPABILITIES,
    PLATFORM_IO_CAPABILITY_SET,
    UNATTENDED_DEFAULT_PLATFORM_IO_CAPABILITIES,
)


class NativeToolCatalogTests(unittest.TestCase):
    def test_platform_catalog_contains_only_controller_owned_io(self):
        self.assertEqual(
            len(PLATFORM_IO_CAPABILITIES),
            len(PLATFORM_IO_CAPABILITY_SET),
        )
        self.assertEqual(
            ("web_search", "market_quote", "cronjob"),
            PLATFORM_IO_CAPABILITIES,
        )
        self.assertFalse({
            "read_file",
            "write_file",
            "execute_code",
            "delegate_task",
            "skill_view",
        } & PLATFORM_IO_CAPABILITY_SET)

    def test_unattended_platform_default_excludes_recursive_scheduling(self):
        self.assertEqual(
            ("web_search", "market_quote"),
            UNATTENDED_DEFAULT_PLATFORM_IO_CAPABILITIES,
        )

    def test_scheduled_tool_compiler_binds_visible_aliases_to_authority(self):
        self.assertEqual(
            canonicalize_scheduled_platform_capabilities(
                [
                    "mcp__chatds-market-data__market_quote",
                    "market_quote",
                ],
                allowed_capabilities=frozenset({"market_quote", "cronjob"}),
            ),
            ("market_quote",),
        )
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            canonicalize_scheduled_platform_capabilities(
                ["web_search"],
                allowed_capabilities=frozenset({"market_quote"}),
            )
        with self.assertRaisesRegex(ValueError, "unknown"):
            canonicalize_scheduled_platform_capabilities(
                ["Bash"],
                allowed_capabilities=PLATFORM_IO_CAPABILITY_SET,
            )

    def test_scheduled_tool_defaults_do_not_widen_explicit_empty_set(self):
        allowed = frozenset({"market_quote", "web_search", "cronjob"})
        self.assertEqual(
            canonicalize_scheduled_platform_capabilities(
                [],
                allowed_capabilities=allowed,
            ),
            (),
        )
        self.assertEqual(
            canonicalize_scheduled_platform_capabilities(
                None,
                allowed_capabilities=allowed,
            ),
            ("web_search", "market_quote"),
        )
        with self.assertRaisesRegex(ValueError, "unknown"):
            canonicalize_scheduled_platform_capabilities(
                ["mcp__foreign__market_quote"],
                allowed_capabilities=frozenset({"market_quote"}),
            )

    def test_deepseek_uses_one_complete_engine_owned_native_tool_graph(self):
        tools = deepseek_harness_native_tools()
        self.assertIs(tools, DEEPSEEK_HARNESS_NATIVE_TOOLS)
        self.assertEqual(len(tools), len(set(tools)))
        self.assertTrue({
            "bash",
            "read",
            "write",
            "skill",
            "subagent",
            "subagent_fork",
            "workflow",
            "ralph",
            "ask_user_question",
            "todo_write",
            "create_goal",
            "web_search",
        }.issubset(tools))
        self.assertNotIn("read_file", tools)
        self.assertNotIn("delegate_task", tools)


if __name__ == "__main__":
    unittest.main()
