import json
import unittest

from run_contract import (
    LifecyclePhase,
    RunContractLedger,
    RunLifecycleMachine,
    TerminalOutcome,
)
from run_contract_adapter import apply_agent_event


SHA_A = "a" * 64
SHA_B = "b" * 64


def _event(
    event_type,
    *,
    run_id="run-1",
    seq=1,
    payload=None,
    **extra,
):
    return {
        "type": "agent_event",
        "event_type": event_type,
        "run_id": run_id,
        "seq": seq,
        "payload": dict(payload or {}),
        **extra,
    }


class RunContractAdapterTests(unittest.TestCase):
    def setUp(self):
        self.machine = RunLifecycleMachine("run-1")
        self.ledger = RunContractLedger("run-1")

    def apply(self, event):
        return apply_agent_event(self.machine, self.ledger, event)

    def test_unknown_event_is_ignored_without_state_change(self):
        before_lifecycle = self.machine.snapshot()
        before_ledger = self.ledger.snapshot()

        result = self.apply(_event("debug.provider.payload"))

        self.assertFalse(result.recognized)
        self.assertFalse(result.applied)
        self.assertEqual("ignored_unknown_event", result.code)
        self.assertEqual(before_lifecycle, self.machine.snapshot())
        self.assertEqual(before_ledger, self.ledger.snapshot())

    def test_existing_verifier_events_drive_strict_lifecycle(self):
        events = [
            _event("run.started", seq=1),
            _event("verifier.requested", seq=2),
            _event(
                "verifier.completed",
                seq=3,
                payload={"needs_more_work": False},
            ),
            _event("run.completed", seq=4),
        ]

        results = [self.apply(event) for event in events]

        self.assertTrue(all(item.applied for item in results))
        self.assertEqual(LifecyclePhase.TERMINAL, self.machine.phase)
        self.assertEqual(
            TerminalOutcome.COMPLETED,
            self.machine.terminal_outcome,
        )

    def test_verifier_followup_and_provisional_terminal_do_not_finish(self):
        self.apply(_event("run.started", seq=1))
        self.apply(_event("verifier.requested", seq=2))
        followup = self.apply(_event(
            "verifier.completed",
            seq=3,
            payload={"needs_more_work": True},
        ))
        provisional = self.apply(_event(
            "run.completed",
            seq=4,
            payload={
                "authoritative": False,
                "provisional_terminal": True,
            },
        ))

        self.assertTrue(followup.applied)
        self.assertEqual(
            "provisional_terminal_observed",
            provisional.code,
        )
        self.assertEqual(LifecyclePhase.EXECUTING, self.machine.phase)

    def test_dispatch_started_then_completed_converges_one_active_receipt(self):
        started = self.apply(_event(
            "tool.dispatch_started",
            seq=1,
            payload={
                "tool_name": "web_search",
                "tool_call_id": "opaque-call-id",
                "actual_dispatch_attempted": True,
                "contract_required": True,
                "invocation_argument_projection": {
                    "safe_sha256": SHA_A,
                },
            },
        ))
        pending = self.ledger.preview_terminal("completed")

        completed_event = _event(
            "tool.completed",
            seq=2,
            payload={
                "tool_name": "web_search",
                "tool_call_id": "opaque-call-id",
                "actual_dispatch_attempted": True,
                "outcome": "success",
            },
        )
        completed = self.apply(completed_event)
        replay = self.apply(completed_event)
        snapshot = self.ledger.snapshot()

        self.assertTrue(started.applied)
        self.assertFalse(pending["completion_allowed"])
        self.assertTrue(completed.applied)
        self.assertTrue(replay.applied)
        self.assertEqual(2, snapshot["entry_count_total"])
        self.assertEqual(1, snapshot["entry_count_active"])
        self.assertEqual(0, snapshot["pending_dispatch_count"])
        self.assertEqual("verified", snapshot["quality"])
        active = [entry for entry in snapshot["entries"] if entry["active"]]
        self.assertTrue(active[0]["required"])
        self.assertEqual(
            SHA_A,
            active[0]["receipt"]["invocation_safe_sha256"],
        )
        rendered = json.dumps(snapshot)
        self.assertNotIn("opaque-call-id", rendered)

    def test_required_tool_failure_blocks_completed_terminal(self):
        self.apply(_event(
            "tool.dispatch_started",
            payload={
                "tool_name": "database_query",
                "tool_call_id": "call-required",
                "actual_dispatch_attempted": True,
                "contract_required": True,
            },
        ))
        self.apply(_event(
            "tool.failed",
            seq=2,
            payload={
                "tool_name": "database_query",
                "tool_call_id": "call-required",
                "actual_dispatch_attempted": True,
                "outcome": "error",
            },
        ))
        self.apply(_event("run.started", seq=3))
        self.apply(_event("verifier.requested", seq=4))
        self.apply(_event("verifier.completed", seq=5))

        rejected = self.apply(_event("run.completed", seq=6))
        failed = self.apply(_event("run.failed", seq=6))

        self.assertFalse(rejected.applied)
        self.assertEqual("completion_contract_failed", rejected.code)
        self.assertTrue(failed.applied)
        self.assertEqual(TerminalOutcome.FAILED, self.machine.terminal_outcome)

    def test_agent_result_maps_quality_and_requires_receipt_for_verified(self):
        verified = self.apply(_event(
            "agent.result",
            payload={
                "node_id": "worker-a",
                "skill_name": "generic-skill",
                "step_type": "worker",
                "completion_quality": "complete",
                "result_receipt_sha256": SHA_A,
            },
        ))
        degraded = self.apply(_event(
            "agent.result",
            seq=2,
            payload={
                "node_id": "worker-b",
                "completion_quality": "complete",
            },
        ))
        snapshot = self.ledger.snapshot()

        self.assertTrue(verified.applied)
        self.assertTrue(degraded.applied)
        self.assertEqual(1, snapshot["active_state_counts"]["verified"])
        self.assertEqual(1, snapshot["active_state_counts"]["degraded"])

    def test_agent_result_retry_supersedes_failed_node_and_allows_completion(self):
        failed = self.apply(_event(
            "agent.result",
            seq=1,
            payload={
                "node_id": "worker-retry",
                "skill_name": "generic-skill",
                "step_type": "worker",
                "status": "failed",
                "attempt": 1,
                "contract_required": True,
                "reason_code": "provider_timeout",
            },
        ))
        blocked = self.ledger.preview_terminal("completed")

        succeeded = self.apply(_event(
            "agent.result",
            seq=2,
            payload={
                "node_id": "worker-retry",
                "status": "completed",
                "revision": 2,
                "result_receipt_sha256": SHA_A,
                "contract_required": True,
            },
        ))
        recovered = self.ledger.preview_terminal("completed")

        self.assertTrue(failed.applied)
        self.assertFalse(blocked["completion_allowed"])
        self.assertTrue(succeeded.applied)
        self.assertTrue(recovered["completion_allowed"])
        self.assertEqual("verified", recovered["quality"])
        self.assertEqual(2, recovered["entry_count_total"])
        self.assertEqual(1, recovered["entry_count_active"])
        self.assertEqual(0, recovered["required_failed_count"])
        active = [
            entry for entry in recovered["entries"] if entry["active"]
        ]
        self.assertEqual(2, active[0]["revision"])
        self.assertEqual(
            "generic-skill",
            active[0]["subject"]["skill_name"],
        )

        self.apply(_event("run.started", seq=10))
        self.apply(_event("verifier.requested", seq=11))
        self.apply(_event("verifier.completed", seq=12))
        completed = self.apply(_event("run.completed", seq=13))
        self.assertTrue(completed.applied)
        self.assertEqual(
            TerminalOutcome.COMPLETED,
            self.machine.terminal_outcome,
        )

    def test_agent_result_same_revision_conflict_fails_closed(self):
        self.apply(_event(
            "agent.result",
            payload={
                "node_id": "worker-conflict",
                "status": "failed",
                "attempt": 1,
            },
        ))

        conflict = self.apply(_event(
            "agent.result",
            seq=2,
            payload={
                "node_id": "worker-conflict",
                "status": "completed",
                "revision": 1,
                "result_receipt_sha256": SHA_A,
            },
        ))

        self.assertFalse(conflict.applied)
        self.assertEqual("invalid_event_contract", conflict.code)
        snapshot = self.ledger.snapshot()
        self.assertEqual(1, snapshot["entry_count_total"])
        self.assertEqual("failed", snapshot["quality"])

    def test_agent_result_rejects_new_out_of_order_revision(self):
        self.apply(_event(
            "agent.result",
            payload={
                "node_id": "worker-order",
                "status": "completed",
                "attempt": 2,
                "result_receipt_sha256": SHA_A,
            },
        ))

        stale = self.apply(_event(
            "agent.result",
            seq=2,
            payload={
                "node_id": "worker-order",
                "status": "failed",
                "revision": 1,
            },
        ))

        self.assertFalse(stale.applied)
        self.assertEqual("invalid_event_contract", stale.code)
        self.assertEqual("verified", self.ledger.snapshot()["quality"])

    def test_artifact_and_evidence_events_use_only_typed_receipts(self):
        artifact = self.apply(_event(
            "artifact.created",
            seq=10,
            payload={
                "path": "reports/final.md",
                "sha256": SHA_A,
                "size_bytes": 2048,
                "source_tool_name": "write_file",
                "contract_required": True,
            },
        ))
        evidence = self.apply(_event(
            "evidence.verified",
            seq=11,
            payload={
                "evidence_id": "registry-record-1",
                "source_kind": "remote-registry",
                "receipt_sha256": SHA_B,
            },
        ))

        snapshot = self.ledger.snapshot()

        self.assertTrue(artifact.applied)
        self.assertTrue(evidence.applied)
        self.assertEqual("verified", snapshot["quality"])
        self.assertEqual(1, snapshot["active_kind_counts"]["artifact"])
        self.assertEqual(1, snapshot["active_kind_counts"]["evidence"])

    def test_secret_bearing_known_event_is_rejected_without_echo(self):
        result = self.apply(_event(
            "artifact.created",
            payload={
                "path": "password=should-not-persist/report.md",
                "sha256": SHA_A,
            },
        ))

        self.assertTrue(result.recognized)
        self.assertFalse(result.applied)
        self.assertEqual("invalid_event_contract", result.code)
        self.assertNotIn(
            "should-not-persist",
            json.dumps(result.as_dict()),
        )
        self.assertEqual(0, self.ledger.snapshot()["entry_count_total"])

        structured = self.apply(_event(
            "evidence.failed",
            seq=2,
            payload={
                "evidence_id": {
                    "password": "also-should-not-persist",
                },
                "source_kind": "registry",
            },
        ))
        self.assertEqual("invalid_event_contract", structured.code)
        self.assertNotIn(
            "also-should-not-persist",
            json.dumps(structured.as_dict()),
        )
        self.assertEqual(0, self.ledger.snapshot()["entry_count_total"])

    def test_event_from_other_run_is_rejected(self):
        result = self.apply(_event(
            "run.started",
            run_id="another-run",
        ))

        self.assertFalse(result.applied)
        self.assertEqual("event_run_id_mismatch", result.code)
        self.assertEqual(LifecyclePhase.PLANNED, self.machine.phase)


if __name__ == "__main__":
    unittest.main()
