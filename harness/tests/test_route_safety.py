import re
import time
import unittest
from unittest import mock

from skills import route_safety
from skills.route_safety import (
    route_pattern_validation_error,
    safe_route_pattern_search,
)


class RouteSafetyTests(unittest.TestCase):
    def test_rejects_overlapping_variable_repeat_frontiers(self):
        unsafe_patterns = (
            "a*a*a*a*a*a*b",
            "[ab]*[bc]*z",
            ".*.*z",
            r"a+\s*a+",
            r"a+\ba+",
            r"(a*)(?:a*)b",
            "[A-Z]+[a-z]+z",
        )

        for pattern in unsafe_patterns:
            with self.subTest(pattern=pattern):
                error = route_pattern_validation_error(pattern)
                self.assertIsNotNone(error)
                self.assertIn("quantifiers", error)

    def test_accepts_common_linear_route_patterns(self):
        safe_patterns = (
            "foo.*bar",
            ".*foo.*bar",
            r"\b(foo|bar)\b",
            r"\b[a-z]{1,32}\b",
            "(?:foo|bar|baz)",
            "items{1,3}",
            "[ab]*[cd]*z",
        )

        for pattern in safe_patterns:
            with self.subTest(pattern=pattern):
                self.assertIsNone(route_pattern_validation_error(pattern))

    def test_rejected_long_no_match_returns_without_running_regex(self):
        started = time.monotonic()
        matched = safe_route_pattern_search(
            "a*a*a*a*a*a*b",
            "a" * 100_000 + "no-match",
        )
        elapsed = time.monotonic() - started

        self.assertFalse(matched)
        # Validation rejects before the stdlib backtracking engine is entered.
        # One second is intentionally generous for slow shared CI runners.
        self.assertLess(elapsed, 1.0)

    def test_validation_and_runtime_share_cached_compilation(self):
        pattern = r"\bclinical.*trial\b"
        real_compile = re.compile
        route_safety._validated_route_pattern.cache_clear()
        try:
            with mock.patch.object(
                route_safety.re,
                "compile",
                wraps=real_compile,
            ) as compile_mock:
                self.assertIsNone(route_pattern_validation_error(pattern))
                self.assertTrue(
                    safe_route_pattern_search(pattern, "Clinical phase II trial")
                )
                self.assertFalse(safe_route_pattern_search(pattern, "unrelated"))
                self.assertEqual(compile_mock.call_count, 1)
        finally:
            route_safety._validated_route_pattern.cache_clear()


if __name__ == "__main__":
    unittest.main()
