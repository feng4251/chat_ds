import unittest

from provider_stream_deadline import (
    MaterialProgressLease,
    build_provider_stream_deadline_plan,
)


def _plan(**overrides):
    values = {
        "estimated_input_tokens": 71_892,
        "max_output_tokens": 8_192,
        "initial_lease_seconds": 600.0,
        "progress_grace_seconds": 180.0,
        "configured_hard_cap_seconds": 1_500.0,
        "caller_hard_cap_seconds": None,
        "input_planning_tokens_per_second": 256.0,
        "output_planning_tokens_per_second": 8.0,
        "planning_safety_factor": 1.10,
        "fixed_overhead_seconds": 30.0,
    }
    values.update(overrides)
    return build_provider_stream_deadline_plan(**values)


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class ProviderStreamDeadlinePlanTests(unittest.TestCase):
    def test_large_request_gets_budgeted_time_without_exceeding_hard_cap(self):
        plan = _plan()

        self.assertGreater(plan.planned_deadline_seconds, 1_400.0)
        self.assertLessEqual(plan.planned_deadline_seconds, 1_500.0)
        self.assertEqual(600.0, plan.initial_lease_seconds)

    def test_explicit_caller_cap_is_authoritative(self):
        plan = _plan(caller_hard_cap_seconds=45.0)

        self.assertEqual(45.0, plan.hard_cap_seconds)
        self.assertEqual(45.0, plan.planned_deadline_seconds)
        self.assertEqual(45.0, plan.initial_lease_seconds)

    def test_small_request_keeps_the_initial_bound(self):
        plan = _plan(
            estimated_input_tokens=100,
            max_output_tokens=128,
        )

        self.assertEqual(600.0, plan.planned_deadline_seconds)

    def test_planned_estimate_does_not_shrink_progress_grace(self):
        plan = _plan(
            estimated_input_tokens=0,
            max_output_tokens=1,
            initial_lease_seconds=20.0,
            progress_grace_seconds=40.0,
            configured_hard_cap_seconds=200.0,
            input_planning_tokens_per_second=1_000_000.0,
            output_planning_tokens_per_second=1_000_000.0,
            planning_safety_factor=1.0,
            fixed_overhead_seconds=1.0,
        )

        self.assertEqual(20.0, plan.planned_deadline_seconds)
        self.assertEqual(40.0, plan.progress_grace_seconds)
        self.assertEqual(200.0, plan.hard_cap_seconds)

    def test_invalid_config_fails_closed(self):
        invalid = {
            "estimated_input_tokens": -1,
            "max_output_tokens": 0,
            "initial_lease_seconds": float("inf"),
            "progress_grace_seconds": 0,
            "configured_hard_cap_seconds": -1,
            "caller_hard_cap_seconds": float("nan"),
            "input_planning_tokens_per_second": 0,
            "output_planning_tokens_per_second": False,
            "planning_safety_factor": 0,
            "fixed_overhead_seconds": 0,
        }
        for field, value in invalid.items():
            with self.subTest(field=field):
                with self.assertRaises((TypeError, ValueError)):
                    _plan(**{field: value})


class MaterialProgressLeaseTests(unittest.TestCase):
    def test_material_progress_can_cross_initial_but_not_hard_deadline(self):
        lease = MaterialProgressLease.start(_plan(), now=100.0)
        self.assertEqual(700.0, lease.deadline)

        self.assertTrue(lease.observe_material_progress(2_000, now=650.0))
        self.assertEqual(830.0, lease.deadline)
        self.assertTrue(lease.observe_material_progress(2_000, now=825.0))
        self.assertEqual(1_005.0, lease.deadline)

        # Repeated genuine progress may use the plan, never exceed it.
        for now in (1_000.0, 1_175.0, 1_350.0, 1_525.0):
            lease.observe_material_progress(1, now=now)
        self.assertLessEqual(lease.deadline, lease.hard_deadline)
        self.assertEqual(
            lease.started_at + lease.plan.hard_cap_seconds,
            lease.hard_deadline,
        )

    def test_planned_checkpoint_is_observed_not_enforced(self):
        plan = _plan(
            estimated_input_tokens=0,
            max_output_tokens=1,
            initial_lease_seconds=120.0,
            progress_grace_seconds=45.0,
            configured_hard_cap_seconds=240.0,
            input_planning_tokens_per_second=1_000_000.0,
            output_planning_tokens_per_second=1_000_000.0,
            planning_safety_factor=1.0,
            fixed_overhead_seconds=1.0,
        )
        clock = _FakeClock(1_000.0)
        lease = MaterialProgressLease.start(plan, now=clock.now)

        self.assertEqual(120.0, plan.planned_deadline_seconds)
        self.assertEqual(1_120.0, lease.deadline)
        self.assertEqual(1_240.0, lease.hard_deadline)

        self.assertTrue(
            lease.observe_material_progress(
                1,
                now=clock.advance(119.0),
            )
        )
        self.assertEqual(1_164.0, lease.deadline)
        metrics = lease.debug_payload(now=clock.advance(2.0))
        self.assertTrue(metrics["planned_budget_crossed"])
        self.assertEqual("progress_lease", metrics["deadline_kind"])

    def test_empty_transport_frames_do_not_renew(self):
        lease = MaterialProgressLease.start(_plan(), now=0.0)

        self.assertFalse(lease.observe_material_progress(0, now=599.0))
        self.assertEqual(600.0, lease.deadline)
        self.assertEqual(0, lease.renewal_count)

    def test_progress_after_hard_deadline_is_inert(self):
        plan = _plan(caller_hard_cap_seconds=10.0)
        lease = MaterialProgressLease.start(plan, now=5.0)

        self.assertFalse(lease.observe_material_progress(10, now=16.0))
        self.assertEqual(15.0, lease.deadline)
        self.assertEqual(0, lease.material_progress_chars)


if __name__ == "__main__":
    unittest.main()
