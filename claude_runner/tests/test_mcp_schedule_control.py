import json
import unittest

from claude_runner import mcp_schedule_control


class ScheduleControlMcpTests(unittest.TestCase):
    def test_bounded_schedule_receipt_preserves_exact_cross_domain_request(self):
        arguments = {
            "name": "Factory sensor watch",
            "prompt": "Read the two selected sensor values and report them.",
            "schedule": "*/10 13-14 12 8 *",
            "timezone": "Asia/Shanghai",
            "max_runs": 12,
            "expires_at": "2026-08-12T15:00:00+08:00",
            "enabled_tools": ["web_search"],
        }
        normalized = mcp_schedule_control.normalize_schedule_create(arguments)
        receipt = json.loads(mcp_schedule_control._accepted_receipt(arguments))

        self.assertEqual(normalized["schedule"], "*/10 13-14 12 8 *")
        self.assertEqual(normalized["max_runs"], 12)
        self.assertEqual(receipt["schema"], "chatds.schedule.accepted.v1")
        self.assertEqual(
            receipt["status"], "accepted_pending_terminal_commit"
        )

    def test_unbounded_or_malformed_control_fields_fail_closed(self):
        base = {
            "name": "Renamed holdout",
            "prompt": "Observe the selected value.",
            "schedule": "*/10 * * * *",
            "timezone": "UTC",
        }
        with self.assertRaisesRegex(ValueError, "max_runs"):
            mcp_schedule_control.normalize_schedule_create({
                **base, "max_runs": 0,
            })
        with self.assertRaisesRegex(ValueError, "request"):
            mcp_schedule_control.normalize_schedule_create({
                **base, "conversation_id": "forged-owner",
            })


if __name__ == "__main__":
    unittest.main()
