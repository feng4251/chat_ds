import json
import unittest

from run_contract import (
    LifecyclePhase,
    QualityState,
    RunContractLedger,
    RunLifecycleMachine,
    TerminalOutcome,
    build_reconciliation_snapshot,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _completed_machine(run_id: str = "run-1") -> RunLifecycleMachine:
    machine = RunLifecycleMachine(run_id)
    assert machine.observe_event("run.started", seq=1).accepted
    assert machine.observe_event("verifier.requested", seq=2).accepted
    assert machine.observe_event("run.committing", seq=3).accepted
    assert machine.observe_event("run.completed", seq=4).accepted
    return machine


class RunLifecycleMachineTests(unittest.TestCase):
    def test_normal_verified_lifecycle_reaches_one_terminal(self):
        machine = _completed_machine()

        self.assertEqual(LifecyclePhase.TERMINAL, machine.phase)
        self.assertEqual(TerminalOutcome.COMPLETED, machine.terminal_outcome)
        snapshot = machine.snapshot()
        self.assertTrue(snapshot["integrity_valid"])
        self.assertEqual(4, snapshot["last_applied_seq"])
        self.assertRegex(snapshot["snapshot_sha256"], r"^[0-9a-f]{64}$")

    def test_verifier_followup_returns_to_execution(self):
        machine = RunLifecycleMachine("run-followup")
        machine.observe_event("run.started", seq=1)
        machine.observe_event("verifier.requested", seq=2)

        decision = machine.observe_event(
            "verifier.followup_requested",
            seq=3,
            verifier_followup=True,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(LifecyclePhase.EXECUTING, machine.phase)
        self.assertTrue(
            machine.observe_event("verifier.requested", seq=4).accepted
        )
        self.assertTrue(
            machine.observe_event("run.committing", seq=5).accepted
        )
        self.assertTrue(
            machine.observe_event("run.completed", seq=6).accepted
        )

    def test_exact_seq_replay_is_idempotent(self):
        machine = RunLifecycleMachine("run-replay")
        first = machine.observe_event("run.started", seq=1)
        replay = machine.observe_event("run.started", seq=1)

        self.assertTrue(first.changed)
        self.assertTrue(replay.accepted)
        self.assertFalse(replay.changed)
        self.assertEqual("idempotent_replay", replay.code)
        self.assertEqual(1, machine.snapshot()["idempotent_replay_count"])

    def test_same_seq_conflict_is_rejected(self):
        machine = RunLifecycleMachine("run-conflict")
        machine.observe_event("run.started", seq=1)

        conflict = machine.observe_event(
            "run.failed",
            seq=1,
        )

        self.assertFalse(conflict.accepted)
        self.assertEqual("seq_conflict", conflict.code)
        self.assertFalse(machine.snapshot()["integrity_valid"])
        self.assertEqual(LifecyclePhase.EXECUTING, machine.phase)

    def test_out_of_order_event_is_rejected(self):
        machine = RunLifecycleMachine("run-order")
        machine.observe_event("run.started", seq=2)

        stale = machine.observe_event("agent.spawned", seq=1)

        self.assertFalse(stale.accepted)
        self.assertEqual("out_of_order", stale.code)
        self.assertEqual(2, machine.last_seen_seq)

    def test_provisional_terminal_does_not_finish_run(self):
        machine = RunLifecycleMachine("run-provisional")
        machine.observe_event("run.started", seq=1)
        provisional = machine.observe_event(
            "run.completed",
            seq=2,
            authoritative=False,
        )

        self.assertTrue(provisional.accepted)
        self.assertFalse(provisional.changed)
        self.assertEqual("provisional_terminal_observed", provisional.code)
        self.assertEqual(LifecyclePhase.EXECUTING, machine.phase)
        self.assertIsNone(machine.terminal_outcome)

        machine.observe_event("verifier.requested", seq=3)
        machine.observe_event("run.committing", seq=4)
        authoritative = machine.observe_event("run.completed", seq=5)
        self.assertTrue(authoritative.changed)
        self.assertEqual(TerminalOutcome.COMPLETED, machine.terminal_outcome)
        self.assertEqual(
            1, machine.snapshot()["provisional_observation_count"]
        )

    def test_terminal_is_monotonic(self):
        machine = _completed_machine("run-terminal")

        reopened = machine.observe_event("run.started", seq=5)
        changed_terminal = machine.observe_event("run.failed", seq=6)

        self.assertFalse(reopened.accepted)
        self.assertFalse(changed_terminal.accepted)
        self.assertEqual("terminal_monotonicity", reopened.code)
        self.assertEqual("terminal_monotonicity", changed_terminal.code)
        self.assertEqual(TerminalOutcome.COMPLETED, machine.terminal_outcome)

    def test_success_before_commit_rejected_but_abort_edge_allowed(self):
        premature = RunLifecycleMachine("run-premature")
        premature.observe_event("run.started", seq=1)
        decision = premature.observe_event("run.completed", seq=2)
        self.assertFalse(decision.accepted)
        self.assertEqual("completion_before_commit", decision.code)

        failed = RunLifecycleMachine("run-failed")
        failed.observe_event("run.started", seq=1)
        decision = failed.observe_event("run.failed", seq=2)
        self.assertTrue(decision.accepted)
        self.assertEqual(TerminalOutcome.FAILED, failed.terminal_outcome)

    def test_verifier_completion_enters_commit_and_skip_is_rejected(self):
        machine = RunLifecycleMachine("run-verifier-complete")
        machine.observe_event("run.started", seq=1)

        skipped = machine.observe_event("run.committing", seq=2)

        self.assertFalse(skipped.accepted)
        self.assertEqual("invalid_transition", skipped.code)

        valid = RunLifecycleMachine("run-verifier-complete-valid")
        valid.observe_event("run.started", seq=1)
        valid.observe_event("verifier.requested", seq=2)
        completed = valid.observe_event("verifier.completed", seq=3)
        self.assertTrue(completed.changed)
        self.assertEqual(LifecyclePhase.COMMITTING, valid.phase)

    def test_exact_replay_of_rejected_transition_stays_rejected(self):
        machine = RunLifecycleMachine("run-rejected-replay")
        machine.observe_event("run.started", seq=1)
        first = machine.observe_event("run.committing", seq=2)
        replay = machine.observe_event("run.committing", seq=2)

        self.assertFalse(first.accepted)
        self.assertFalse(replay.accepted)
        self.assertEqual("invalid_transition", replay.code)
        self.assertEqual(
            [{"code": "invalid_transition", "count": 1}],
            machine.snapshot()["rejections"],
        )


class RunContractLedgerTests(unittest.TestCase):
    def test_verified_receipts_produce_stable_secret_free_snapshot(self):
        ledger = RunContractLedger("run-verified")
        ledger.record_node(
            "worker-a",
            state=QualityState.VERIFIED,
            skill_name="generic-skill",
            step_type="worker",
            result_receipt_sha256=SHA_A,
        )
        ledger.record_dispatch(
            "skill_http_get",
            "runtime-call-1",
            state=QualityState.VERIFIED,
            actual_dispatch_attempted=True,
            invocation_safe_sha256=SHA_B,
            receipt_sha256=SHA_C,
        )
        ledger.record_evidence(
            "registry-record",
            state=QualityState.VERIFIED,
            source_kind="remote-registry",
            receipt_sha256=SHA_C,
        )
        ledger.record_artifact(
            "reports/final.md",
            state=QualityState.VERIFIED,
            sha256=SHA_A,
            size_bytes=4096,
            source_tool="write_file",
        )

        first = ledger.snapshot()
        second = ledger.snapshot()

        self.assertEqual(first, second)
        self.assertEqual("verified", first["quality"])
        self.assertEqual(4, first["entry_count_active"])
        rendered = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("runtime-call-1", rendered)
        self.assertNotIn("http://", rendered)
        self.assertRegex(first["snapshot_sha256"], r"^[0-9a-f]{64}$")

    def test_quality_vocabulary_and_required_precedence(self):
        ledger = RunContractLedger("run-quality")
        ledger.record_node(
            "verified",
            state="verified",
            result_receipt_sha256=SHA_A,
        )
        ledger.record_evidence(
            "unsupported-source",
            state="unsupported",
            source_kind="database",
            reason_code="connector_not_declared",
        )
        ledger.record_evidence(
            "conflicting-source",
            state="conflicted",
            source_kind="registry",
            reason_code="source_values_conflict",
        )

        snapshot = ledger.snapshot()

        self.assertEqual("conflicted", snapshot["quality"])
        self.assertEqual(1, snapshot["active_state_counts"]["verified"])
        self.assertEqual(1, snapshot["active_state_counts"]["unsupported"])
        self.assertEqual(1, snapshot["active_state_counts"]["conflicted"])

    def test_optional_failure_degrades_instead_of_failing_run(self):
        ledger = RunContractLedger("run-optional")
        ledger.record_node(
            "required",
            state="verified",
            result_receipt_sha256=SHA_A,
        )
        ledger.record_evidence(
            "optional-source",
            state="failed",
            source_kind="fallback",
            required=False,
            reason_code="optional_lookup_failed",
        )

        snapshot = ledger.seal("completed")

        self.assertEqual("degraded", snapshot["quality"])
        self.assertTrue(snapshot["completion_allowed"])

    def test_latest_revision_is_active_but_history_remains_hashed(self):
        ledger = RunContractLedger("run-revision")
        ledger.record_node(
            "worker-a",
            state="failed",
            attempt=1,
            reason_code="provider_timeout",
        )
        ledger.record_node(
            "worker-a",
            state="verified",
            attempt=2,
            result_receipt_sha256=SHA_A,
        )

        snapshot = ledger.snapshot()

        self.assertEqual("verified", snapshot["quality"])
        self.assertEqual(2, snapshot["entry_count_total"])
        self.assertEqual(1, snapshot["entry_count_active"])
        active = [entry for entry in snapshot["entries"] if entry["active"]]
        self.assertEqual(2, active[0]["revision"])

    def test_non_dispatch_intent_creates_no_receipt(self):
        ledger = RunContractLedger("run-no-dispatch")
        result = ledger.record_dispatch(
            "write_file",
            "call-never-entered",
            state="verified",
            actual_dispatch_attempted=False,
        )

        self.assertIsNone(result)
        self.assertEqual(0, ledger.snapshot()["entry_count_total"])

    def test_bounded_mcp_public_tool_name_fits_receipt_contract(self):
        ledger = RunContractLedger("run-long-mcp-name")
        public_name = "mcp_" + ("a" * 508)

        ledger.record_dispatch(
            public_name,
            "call-long-name",
            state="verified",
            actual_dispatch_attempted=True,
            receipt_sha256=SHA_A,
        )

        self.assertEqual(
            public_name,
            ledger.snapshot()["entries"][0]["subject"]["tool_name"],
        )
        with self.assertRaisesRegex(ValueError, "exceeds 512"):
            ledger.record_dispatch(
                public_name + "a",
                "call-too-long",
                state="failed",
                actual_dispatch_attempted=True,
            )

    def test_bounded_snapshot_prioritizes_active_gaps_and_keeps_counts(self):
        ledger = RunContractLedger(
            "run-bounded",
            max_snapshot_entries=3,
        )
        for index in range(8):
            ledger.record_node(
                f"worker-{index}",
                state="verified",
                result_receipt_sha256=SHA_A,
            )
        ledger.record_evidence(
            "missing-source",
            state="unavailable",
            source_kind="registry",
            reason_code="upstream_unavailable",
        )
        ledger.record_artifact(
            "report.md",
            state="failed",
            reason_code="artifact_contract_failed",
        )

        snapshot = ledger.snapshot()

        self.assertEqual(10, snapshot["entry_count_total"])
        self.assertEqual(3, snapshot["entries_included"])
        self.assertEqual(7, snapshot["entries_omitted"])
        self.assertEqual("failed", snapshot["quality"])
        states = {entry["state"] for entry in snapshot["entries"]}
        self.assertIn("failed", states)
        self.assertIn("unavailable", states)
        self.assertFalse(snapshot["required_failed_omitted"])
        self.assertRegex(snapshot["entries_sha256"], r"^[0-9a-f]{64}$")

    def test_secret_like_identifiers_and_freeform_reason_are_rejected(self):
        ledger = RunContractLedger("run-secret")
        with self.assertRaisesRegex(ValueError, "credential"):
            ledger.record_evidence(
                "password=do-not-store",
                state="failed",
                source_kind="registry",
            )
        with self.assertRaisesRegex(ValueError, "machine code"):
            ledger.record_evidence(
                "source",
                state="failed",
                source_kind="registry",
                reason_code="The server returned a long prose error",
            )
        with self.assertRaisesRegex(ValueError, "URL|workspace-relative"):
            ledger.record_artifact(
                "https://example.test/report.md",
                state="failed",
            )
        with self.assertRaisesRegex(ValueError, "not a URL"):
            ledger.record_evidence(
                "https://example.test/raw-result",
                state="failed",
                source_kind="registry",
            )

    def test_verified_states_require_content_receipts(self):
        ledger = RunContractLedger("run-proof-required")
        with self.assertRaisesRegex(ValueError, "node receipt"):
            ledger.record_node("worker", state="verified")
        with self.assertRaisesRegex(ValueError, "dispatch receipt"):
            ledger.record_dispatch(
                "web_search",
                "call-1",
                state="verified",
                actual_dispatch_attempted=True,
            )
        with self.assertRaisesRegex(ValueError, "evidence receipt"):
            ledger.record_evidence(
                "source-1",
                state="verified",
                source_kind="database",
            )

    def test_evidence_sources_are_independent_revision_streams(self):
        ledger = RunContractLedger("run-multi-source")
        ledger.record_evidence(
            "record-1",
            state="unavailable",
            source_kind="source-a",
            reason_code="upstream_unavailable",
        )
        ledger.record_evidence(
            "record-1",
            state="verified",
            source_kind="source-b",
            receipt_sha256=SHA_A,
        )

        snapshot = ledger.snapshot()

        self.assertEqual(2, snapshot["entry_count_active"])
        self.assertEqual("unavailable", snapshot["quality"])

    def test_conflicting_same_revision_receipt_is_rejected(self):
        ledger = RunContractLedger("run-conflicting-receipt")
        ledger.record_node("worker", state="failed", attempt=1)

        with self.assertRaisesRegex(ValueError, "conflicting receipt"):
            ledger.record_node(
                "worker",
                state="verified",
                attempt=1,
                result_receipt_sha256=SHA_A,
            )

    def test_seal_is_idempotent_and_outcome_is_immutable(self):
        ledger = RunContractLedger("run-seal")
        ledger.record_node(
            "worker",
            state="verified",
            result_receipt_sha256=SHA_A,
        )

        first = ledger.seal("completed")
        replay = ledger.seal("completed")

        self.assertEqual(first, replay)
        with self.assertRaisesRegex(RuntimeError, "different terminal"):
            ledger.seal("failed")
        with self.assertRaisesRegex(RuntimeError, "sealed"):
            ledger.record_node(
                "late",
                state="verified",
                result_receipt_sha256=SHA_A,
            )

    def test_failed_terminal_forces_failed_quality(self):
        ledger = RunContractLedger("run-runtime-failed")
        ledger.record_node(
            "worker",
            state="verified",
            result_receipt_sha256=SHA_A,
        )

        snapshot = ledger.seal("failed")

        self.assertEqual("failed", snapshot["quality"])


class RunReconciliationTests(unittest.TestCase):
    def test_terminal_reconciliation_seals_and_hashes_both_contracts(self):
        machine = _completed_machine("run-reconcile")
        ledger = RunContractLedger("run-reconcile")
        ledger.record_node(
            "worker",
            state="verified",
            result_receipt_sha256=SHA_A,
        )

        first = build_reconciliation_snapshot(machine, ledger)
        second = build_reconciliation_snapshot(machine, ledger)

        self.assertEqual(first, second)
        self.assertTrue(first["terminal"])
        self.assertTrue(first["reconciliation_valid"])
        self.assertEqual("verified", first["quality"])
        self.assertRegex(
            first["reconciliation_sha256"], r"^[0-9a-f]{64}$"
        )

    def test_completed_run_with_required_failure_fails_reconciliation(self):
        machine = _completed_machine("run-contract-failed")
        ledger = RunContractLedger("run-contract-failed")
        ledger.record_artifact(
            "final.md",
            state="failed",
            reason_code="artifact_verifier_failed",
        )

        snapshot = build_reconciliation_snapshot(machine, ledger)

        self.assertEqual("failed", snapshot["quality"])
        self.assertFalse(snapshot["quality_ledger"]["completion_allowed"])
        self.assertFalse(snapshot["reconciliation_valid"])

    def test_lifecycle_integrity_failure_invalidates_reconciliation(self):
        machine = RunLifecycleMachine("run-bad-seq")
        machine.observe_event("run.started", seq=2)
        machine.observe_event("agent.spawned", seq=1)
        machine.observe_event("run.failed", seq=3)
        ledger = RunContractLedger("run-bad-seq")

        snapshot = build_reconciliation_snapshot(machine, ledger)

        self.assertFalse(snapshot["lifecycle"]["integrity_valid"])
        self.assertFalse(snapshot["reconciliation_valid"])
        self.assertEqual("failed", snapshot["quality"])

    def test_run_ids_and_terminal_outcomes_must_match(self):
        machine = _completed_machine("run-a")
        other = RunContractLedger("run-b")
        with self.assertRaisesRegex(ValueError, "run_id"):
            build_reconciliation_snapshot(machine, other)

        ledger = RunContractLedger("run-a")
        ledger.seal("failed")
        with self.assertRaisesRegex(RuntimeError, "different terminal"):
            build_reconciliation_snapshot(machine, ledger)


if __name__ == "__main__":
    unittest.main()
