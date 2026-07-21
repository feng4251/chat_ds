import unittest

from workspace_patterns import (
    WorkspacePatternError,
    normalize_workspace_pattern,
    workspace_pattern_matches,
)


class WorkspacePatternTests(unittest.TestCase):
    def test_root_placeholder_never_matches_nested_artifact(self):
        self.assertTrue(
            workspace_pattern_matches(
                "GAL3_AD_FULL.md",
                "{PROJECT}_FULL.md",
            )
        )
        self.assertTrue(
            workspace_pattern_matches(
                "gal3_ad_full.MD",
                "<PROJECT>_FULL.md",
            )
        )
        self.assertFalse(
            workspace_pattern_matches(
                "nested/GAL3_AD_FULL.md",
                "{PROJECT}_FULL.md",
            )
        )

    def test_ordinary_globs_do_not_cross_segments(self):
        self.assertTrue(workspace_pattern_matches("reports/a.md", "reports/*.md"))
        self.assertFalse(
            workspace_pattern_matches("reports/a/b.md", "reports/*.md")
        )
        self.assertTrue(
            workspace_pattern_matches("reports/a/b.md", "reports/**/*.md")
        )
        self.assertTrue(
            workspace_pattern_matches("reports/b.md", "reports/**/*.md")
        )
        self.assertTrue(workspace_pattern_matches("a1.txt", "a[0-9].txt"))
        self.assertFalse(workspace_pattern_matches("dir/a1.txt", "a?.txt"))

    def test_unsafe_or_ambiguous_patterns_fail_closed(self):
        for value in (
            "/absolute.md",
            "./report.md",
            "reports/../report.md",
            "reports//report.md",
            "reports/foo**bar.md",
            "reports/**",
            "file:///tmp/report.md",
        ):
            with self.subTest(value=value):
                with self.assertRaises(WorkspacePatternError):
                    normalize_workspace_pattern(value)
                self.assertFalse(workspace_pattern_matches("report.md", value))


if __name__ == "__main__":
    unittest.main()
