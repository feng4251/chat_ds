from __future__ import annotations

import unittest

from failure_taxonomy import (
    build_failure_fingerprint,
    common_mode_breaker_eligible,
)


class FailureTaxonomyTests(unittest.TestCase):
    def test_dynamic_ids_paths_urls_and_numbers_share_one_fingerprint(self):
        first = build_failure_fingerprint(
            failure_class="contract_validation",
            terminal_reason="receipt-mismatch",
            error=(
                "Run a3f33f28-40dc-4ba8-8415-5ccf12d92fa8 failed at "
                "/tmp/session-a/result-17.json for https://api.a.test/x/17"
            ),
        )
        second = build_failure_fingerprint(
            failure_class="contract_validation",
            terminal_reason="receipt_mismatch",
            error=(
                "Run 67d2417a-b11d-4604-9945-95f9ed045af8 failed at "
                "/tmp/session-b/result-88.json for https://api.b.test/y/88"
            ),
        )

        self.assertEqual("validator", first["failure_origin"])
        self.assertEqual(
            first["failure_fingerprint"],
            second["failure_fingerprint"],
        )
        self.assertTrue(first["common_mode_breaker_eligible"])

    def test_model_and_provider_failures_are_not_common_mode_eligible(self):
        for failure_class, expected_origin in (
            ("model_output_limit", "model"),
            ("agent_contract_noncompliance", "model"),
            ("provider_protocol", "provider"),
            ("transient_external", "external"),
        ):
            with self.subTest(failure_class=failure_class):
                receipt = build_failure_fingerprint(
                    failure_class=failure_class,
                    terminal_reason="bounded_failure",
                    error="one bounded failure",
                )
                self.assertEqual(
                    expected_origin,
                    receipt["failure_origin"],
                )
                self.assertFalse(
                    common_mode_breaker_eligible(
                        receipt["failure_origin"],
                        receipt["failure_fingerprint"],
                    )
                )

    def test_different_validator_shapes_do_not_collapse(self):
        first = build_failure_fingerprint(
            failure_class="contract_validation",
            terminal_reason="receipt_mismatch",
            error="required field alpha is missing",
        )
        second = build_failure_fingerprint(
            failure_class="contract_validation",
            terminal_reason="receipt_mismatch",
            error="declared digest differs from frozen plan",
        )
        self.assertNotEqual(
            first["failure_fingerprint"],
            second["failure_fingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
