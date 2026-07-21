import json
import unittest

from capability_invocation_ledger import (
    CapabilityInvocationLedger,
    invocation_ledger_prompt,
    safe_argument_projection,
)


def _event(
    seq: int,
    event_type: str,
    call_id: str,
    tool_name: str,
    *,
    args: dict | None = None,
    outcome: str = "success",
) -> dict:
    payload = {
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "actual_dispatch_attempted": True,
    }
    if event_type == "tool.dispatch_started":
        payload["invocation_argument_projection"] = safe_argument_projection(
            args or {}
        )
    else:
        payload["outcome"] = outcome
    return {
        "type": "agent_event",
        "event_type": event_type,
        "run_id": "run-cross-domain",
        "seq": seq,
        "payload": payload,
    }


class CapabilityInvocationLedgerTests(unittest.TestCase):
    def test_ten_cross_domain_calls_report_nine_success_one_failure(self):
        ledger = CapabilityInvocationLedger()
        tools = ["catalog_lookup", "document_search", "metrics_reader"]
        queries = [f"bounded request {index}" for index in range(10)]
        seq = 0
        for index, query in enumerate(queries):
            call_id = f"call-{index}"
            tool_name = tools[index % len(tools)]
            seq += 1
            ledger.observe_event(_event(
                seq,
                "tool.dispatch_started",
                call_id,
                tool_name,
                args={"query": query, "limit": 5},
            ))
            seq += 1
            ledger.observe_event(_event(
                seq,
                "tool.completed" if index != 7 else "tool.failed",
                call_id,
                tool_name,
                outcome="success" if index != 7 else "error",
            ))

        snapshot = ledger.snapshot()
        self.assertEqual(10, snapshot["attempted"])
        self.assertEqual(9, snapshot["succeeded"])
        self.assertEqual(1, snapshot["failed"])
        self.assertEqual(0, snapshot["pending"])
        self.assertTrue(snapshot["count_invariant_valid"])
        self.assertFalse(snapshot["ordered_calls_truncated"])
        self.assertEqual("complete_ordered_list", snapshot["ordered_calls_semantics"])
        self.assertEqual(queries, [
            item["argument_summary"]["query"]
            for item in snapshot["ordered_calls"]
        ])
        self.assertEqual(
            ["succeeded"] * 7 + ["failed"] + ["succeeded"] * 2,
            [item["status"] for item in snapshot["ordered_calls"]],
        )
        self.assertEqual(10, sum(item["attempted"] for item in snapshot["by_tool"]))
        self.assertEqual(9, sum(item["succeeded"] for item in snapshot["by_tool"]))
        self.assertEqual(1, sum(item["failed"] for item in snapshot["by_tool"]))

        prompt = invocation_ledger_prompt(snapshot)
        self.assertIn("CAPABILITY_INVOCATION_LEDGER_JSON", prompt)
        self.assertIn('"attempted":10', prompt)
        self.assertIn('"succeeded":9', prompt)
        self.assertIn('"failed":1', prompt)
        self.assertIn("must not be replaced by a smaller model-estimated count", prompt)
        self.assertIn("must not be reported as a subset", prompt)
        for query in queries:
            self.assertIn(query, prompt)

    def test_duplicate_lifecycle_observation_counts_dispatch_once(self):
        ledger = CapabilityInvocationLedger()
        started = _event(
            4,
            "tool.dispatch_started",
            "same-call",
            "translation_lookup",
            args={"query": "hello"},
        )
        completed = _event(
            5,
            "tool.completed",
            "same-call",
            "translation_lookup",
        )
        ledger.observe_event(started)
        ledger.observe_event(started)
        # A second boundary event with another seq but the same active call ID
        # is still the same handler lifecycle.
        duplicate_boundary = dict(started)
        duplicate_boundary["seq"] = 40
        ledger.observe_event(duplicate_boundary)
        ledger.observe_event(completed)
        ledger.observe_event(completed)
        # Some lifecycle relays assign a fresh seq while forwarding the same
        # already-terminal call. The stable call ID remains authoritative.
        duplicate_after_terminal = dict(started)
        duplicate_after_terminal["seq"] = 400
        ledger.observe_event(duplicate_after_terminal)
        duplicate_completed_after_terminal = dict(completed)
        duplicate_completed_after_terminal["seq"] = 500
        ledger.observe_event(duplicate_completed_after_terminal)

        snapshot = ledger.snapshot()
        self.assertEqual(1, snapshot["attempted"])
        self.assertEqual(1, snapshot["succeeded"])
        self.assertEqual(0, snapshot["failed"])
        self.assertEqual(1, len(snapshot["ordered_calls"]))

    def test_preflight_only_terminal_is_not_a_dispatch(self):
        ledger = CapabilityInvocationLedger()
        event = _event(
            1,
            "tool.failed",
            "preflight-only",
            "repository_reader",
            outcome="error",
        )
        event["payload"]["actual_dispatch_attempted"] = False
        ledger.observe_event(event)
        self.assertEqual(0, ledger.snapshot()["attempted"])

    def test_argument_projection_redacts_secrets_and_literal_payloads(self):
        projection = safe_argument_projection({
            "query": "public catalog term",
            "api_key": "never-persist-this",
            "url": (
                "https://alice:password@example.test/items?"
                "pageToken=NEXT&access_token=never-persist-this#fragment"
            ),
            "content": "large private response body",
            "nested": {"authorization": "Bearer never-persist-this"},
        })
        rendered = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        self.assertIn("public catalog term", rendered)
        self.assertIn("pageToken=NEXT", rendered)
        self.assertIn("[redacted]", rendered)
        self.assertIn("withheld", rendered)
        self.assertNotIn("never-persist-this", rendered)
        self.assertNotIn("alice", rendered)
        self.assertNotIn("large private response body", rendered)
        self.assertTrue(projection["truncated_or_redacted"])

    def test_non_string_mapping_key_keeps_its_safe_value(self):
        projection = safe_argument_projection({7: "seven"})
        self.assertEqual("seven", projection["summary"]["7"])

    def test_truncated_ordered_calls_never_change_complete_counts(self):
        ledger = CapabilityInvocationLedger(max_call_entries=2)
        for index in range(4):
            call_id = f"bounded-{index}"
            ledger.record_dispatch(
                "generic_reader",
                call_id,
                argument_projection=safe_argument_projection({"index": index}),
            )
            ledger.record_outcome(call_id, succeeded=index != 3)
        snapshot = ledger.snapshot()
        self.assertEqual(4, snapshot["attempted"])
        self.assertEqual(3, snapshot["succeeded"])
        self.assertEqual(1, snapshot["failed"])
        self.assertEqual(2, snapshot["ordered_calls_included"])
        self.assertEqual(2, snapshot["ordered_calls_omitted"])
        self.assertTrue(snapshot["ordered_calls_truncated"])
        self.assertEqual("ordered_prefix_only", snapshot["ordered_calls_semantics"])
        prompt = invocation_ledger_prompt(snapshot)
        self.assertIn("only the stated ordered prefix", prompt)
        self.assertIn("never present that prefix as the full set", prompt)


if __name__ == "__main__":
    unittest.main()
