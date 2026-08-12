import unittest
from datetime import datetime, timezone

from schedule_spec import ScheduleSpecError, resolve_schedule_spec


class ScheduleSpecTests(unittest.TestCase):
    NOW = datetime(2026, 8, 12, 9, 15, tzinfo=timezone.utc)

    def test_expired_factory_schedule_is_rejected_before_acceptance(self):
        with self.assertRaisesRegex(
            ScheduleSpecError,
            "no occurrence on or before expires_at",
        ) as raised:
            resolve_schedule_spec(
                "*/2 * * * *",
                "UTC",
                expires_at="2026-08-12T08:55:00Z",
                now=self.NOW,
            )
        self.assertEqual(
            raised.exception.code,
            "schedule_no_occurrence_before_expiry",
        )

    def test_future_expiry_before_next_warehouse_cron_is_rejected(self):
        with self.assertRaises(ScheduleSpecError) as raised:
            resolve_schedule_spec(
                "0 12 * * *",
                "UTC",
                expires_at="2026-08-12T09:59:00+00:00",
                now=self.NOW,
            )
        self.assertEqual(
            raised.exception.code,
            "schedule_no_occurrence_before_expiry",
        )

    def test_boundary_occurrence_and_renamed_interval_are_preserved(self):
        resolved = resolve_schedule_spec(
            "every 2m",
            "UTC",
            expires_at="2026-08-12T09:17:00Z",
            now=self.NOW,
        )
        self.assertEqual((resolved.kind, resolved.value), ("interval", "120"))
        self.assertEqual(
            resolved.next_run_at,
            datetime(2026, 8, 12, 9, 17),
        )
        self.assertEqual(resolved.next_run_at, resolved.expires_at)

    def test_zero_duration_and_naive_expiry_fail_closed(self):
        with self.assertRaises(ScheduleSpecError) as duration:
            resolve_schedule_spec("every 0m", "UTC", now=self.NOW)
        self.assertEqual(duration.exception.code, "invalid_schedule_duration")
        with self.assertRaises(ScheduleSpecError) as expiry:
            resolve_schedule_spec(
                "every 5m",
                "UTC",
                expires_at="2026-08-12T10:00:00",
                now=self.NOW,
            )
        self.assertEqual(
            expiry.exception.code,
            "schedule_expiry_timezone_missing",
        )


if __name__ == "__main__":
    unittest.main()
