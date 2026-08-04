import json
import unittest

from provider_transcript import (
    align_tool_round_boundary,
    audit_provider_transcript,
    canonicalize_legacy_provider_transcript,
    close_active_tool_round,
    project_unique_tool_call_ids,
    tool_round_spans,
)
from transports.chat_completions import ChatCompletionsTransport


def _assistant(*call_ids):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "generic_tool",
                    "arguments": json.dumps({"value": call_id}),
                },
            }
            for call_id in call_ids
        ],
    }


def _result(call_id, *, status="success"):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps({"status": status}),
    }


class ProviderTranscriptTests(unittest.TestCase):
    def test_parallel_batch_requires_contiguous_exact_results(self):
        messages = [
            {"role": "user", "content": "run both"},
            _assistant("call-a", "call-b"),
            _result("call-a"),
            _result("call-b"),
            {"role": "user", "content": "post-round guidance"},
        ]

        audit = audit_provider_transcript(messages)

        self.assertTrue(audit.valid, audit.as_dict())
        self.assertEqual(1, audit.tool_round_count)
        self.assertEqual(2, audit.tool_call_count)
        self.assertEqual(2, audit.tool_result_count)

    def test_interleaved_guidance_is_rejected_by_strict_audit(self):
        messages = [
            _assistant("call-a", "call-b"),
            _result("call-a"),
            {"role": "user", "content": "too early"},
            _result("call-b"),
        ]

        audit = audit_provider_transcript(messages)

        self.assertFalse(audit.valid)
        self.assertIn(
            "tool_result_batch_interleaved",
            {issue.code for issue in audit.issues},
        )
        self.assertIn(
            "orphan_tool_result",
            {issue.code for issue in audit.issues},
        )

    def test_legacy_interleaving_is_hoisted_without_inventing_results(self):
        messages = [
            _assistant("call-a", "call-b"),
            _result("call-a"),
            {"role": "user", "content": "workflow frontier"},
            _result("call-b"),
            {"role": "assistant", "content": "next round"},
        ]

        repaired, report = canonicalize_legacy_provider_transcript(messages)

        self.assertTrue(report.changed)
        self.assertEqual(
            ["assistant", "tool", "tool", "user", "assistant"],
            [message["role"] for message in repaired],
        )
        self.assertEqual(
            ["call-a", "call-b"],
            [repaired[1]["tool_call_id"], repaired[2]["tool_call_id"]],
        )
        self.assertEqual("workflow frontier", repaired[3]["content"])
        self.assertTrue(audit_provider_transcript(repaired).valid)

    def test_unresolved_legacy_call_is_quarantined_not_faked(self):
        messages = [
            _assistant("call-complete", "call-unknown-effect"),
            _result("call-complete"),
            {"role": "user", "content": "resume safely"},
        ]

        repaired, report = canonicalize_legacy_provider_transcript(messages)

        self.assertEqual(1, report.removed_tool_calls)
        self.assertEqual(
            ["call-complete"],
            [call["id"] for call in repaired[0]["tool_calls"]],
        )
        self.assertFalse(any(
            message.get("tool_call_id") == "call-unknown-effect"
            for message in repaired
        ))
        self.assertTrue(audit_provider_transcript(repaired).valid)

    def test_duplicate_and_orphan_ids_are_quarantined(self):
        messages = [
            _assistant("call-a", "call-a"),
            _result("call-a"),
            _result("call-a"),
            _result("orphan"),
        ]

        repaired, report = canonicalize_legacy_provider_transcript(messages)

        self.assertGreaterEqual(report.duplicate_tool_call_ids, 1)
        self.assertGreaterEqual(report.removed_tool_results, 2)
        self.assertTrue(audit_provider_transcript(repaired).valid)

    def test_active_abort_records_not_dispatched_then_guidance(self):
        conversation = [
            {"role": "user", "content": "perform"},
            _assistant("call-a", "call-b", "call-c"),
            _result("call-a", status="error"),
        ]

        report = close_active_tool_round(
            conversation,
            1,
            post_round_user_messages=["bounded recovery guidance"],
            abort_reason="first_call_failed_closed",
        )

        self.assertEqual(
            ("call-b", "call-c"), report.synthetic_not_dispatched_ids
        )
        self.assertEqual(
            ["user", "assistant", "tool", "tool", "tool", "user"],
            [message["role"] for message in conversation],
        )
        self.assertTrue(audit_provider_transcript(conversation).valid)
        for message in conversation[3:5]:
            payload = json.loads(message["content"])
            self.assertFalse(payload["request_sent"])
            self.assertFalse(payload["actual_dispatch_attempted"])
            self.assertEqual(
                "tool_batch_aborted_before_dispatch", payload["error_code"]
            )

    def test_normal_close_refuses_missing_results(self):
        conversation = [_assistant("call-a", "call-b"), _result("call-a")]

        with self.assertRaisesRegex(ValueError, "missing results"):
            close_active_tool_round(conversation, 0)

    def test_compaction_boundaries_never_split_tool_round(self):
        messages = [
            {"role": "user", "content": "start"},
            _assistant("call-a", "call-b"),
            _result("call-a"),
            _result("call-b"),
            {"role": "user", "content": "continue"},
        ]

        self.assertEqual(((1, 4),), tool_round_spans(messages))
        self.assertEqual(
            4,
            align_tool_round_boundary(messages, 2, direction="forward"),
        )
        self.assertEqual(
            1,
            align_tool_round_boundary(messages, 3, direction="backward"),
        )
        self.assertEqual(
            4,
            align_tool_round_boundary(messages, 4, direction="backward"),
        )

    def test_transport_refuses_invalid_transcript_before_sdk_dispatch(self):
        transport = ChatCompletionsTransport()
        messages = [
            _assistant("call-a", "call-b"),
            _result("call-a"),
            {"role": "user", "content": "interleaved"},
            _result("call-b"),
        ]

        with self.assertRaisesRegex(ValueError, "invalid provider transcript"):
            transport.build_kwargs("generic-model", messages)

    def test_reused_later_round_id_is_projected_without_changing_effect_data(self):
        messages = [
            _assistant("provider-reused-id"),
            _result("provider-reused-id"),
            {"role": "user", "content": "next"},
            _assistant("provider-reused-id"),
            _result("provider-reused-id", status="error"),
        ]

        projected, report = project_unique_tool_call_ids(messages)

        first_id = projected[0]["tool_calls"][0]["id"]
        second_id = projected[3]["tool_calls"][0]["id"]
        self.assertEqual("provider-reused-id", first_id)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(second_id, projected[4]["tool_call_id"])
        self.assertEqual("error", json.loads(projected[4]["content"])["status"])
        self.assertEqual(1, report.renamed_tool_call_ids)
        self.assertTrue(audit_provider_transcript(projected).valid)


if __name__ == "__main__":
    unittest.main()
