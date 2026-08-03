import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_loop import _artifact_payloads_from_tool_result
from delegated_result_contract import (
    audit_raw_tool_protocol as _raw_pseudo_tool_protocol_audit,
    canonical_result_fields_footer_from_json,
    canonical_result_fields_footer_from_internal_submitter_json,
    extract_canonical_result_fields_footer,
    strip_result_fields_candidate_tail,
)
from tools.context import ToolContext
from tools.delegation import (
    DELEGATE_TASK_SCHEMA,
    _adaptive_delegate_output_tokens,
    _merge_child_usage,
    _child_failure_fields,
    _canonicalize_machine_gap_ledger,
    _canonicalize_machine_knowledge_gate_check_ledger,
    _canonicalize_duplicate_completion_quality_ledgers,
    _completion_quality_declaration,
    _content_declares_degraded_completion,
    _exact_capability_gap_ledger_error,
    _exact_knowledge_gate_gap_ledger_error,
    _result_field_audit,
    _run_child,
    _strict_result_field_schema,
)


def _context(*tools: str, event_sink=None) -> ToolContext:
    return ToolContext(
        user_id="typed-user",
        session_id="typed-session",
        model_id="model",
        provider_config={
            "base_url": "http://example",
            "api_model": "model",
            "context_length": 303_872,
        },
        enabled_tools=tools,
        run_id="parent",
        root_run_id="root",
        event_sink=event_sink,
    )


def _tool_started(tool_name: str, call_id: str, **args) -> dict:
    return {
        "type": "agent_event",
        "event_type": "tool.started",
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "payload": {
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "args_compacted": args,
        },
    }


def _tool_completed(tool_name: str, call_id: str) -> dict:
    return {
        "type": "agent_event",
        "event_type": "tool.completed",
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "payload": {
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "outcome": "success",
        },
    }


def _legacy_envelope_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "value": {},
            "value_summary": {},
            "provenance": {},
            "reason": {},
        },
    }


class DelegationTypedResultTests(unittest.IsolatedAsyncioTestCase):
    def test_delegate_output_budget_scales_with_contract_shape(self):
        self.assertEqual(
            8_192,
            _adaptive_delegate_output_tokens(["result"], {"result": "string"}),
        )
        medium_fields = [f"field_{index}" for index in range(7)]
        self.assertEqual(
            16_384,
            _adaptive_delegate_output_tokens(
                medium_fields,
                {field: {"type": "string"} for field in medium_fields},
            ),
        )
        large_fields = [f"field_{index}" for index in range(22)]
        self.assertEqual(
            32_768,
            _adaptive_delegate_output_tokens(
                large_fields,
                {field: {"type": "object"} for field in large_fields},
            ),
        )

    def test_child_usage_merge_is_monotonic_alias_aware_and_idempotent(self):
        usage = _merge_child_usage({}, {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
        })
        self.assertEqual({
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        }, usage)
        self.assertEqual(usage, _merge_child_usage(usage, {
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        }))
        self.assertEqual({
            "input_tokens": 125,
            "output_tokens": 35,
            "total_tokens": 160,
        }, _merge_child_usage(usage, {
            "input_tokens": 125,
            "output_tokens": 35,
            "total_tokens": 10,
        }))
        self.assertEqual({
            "input_tokens": 140,
            "output_tokens": 40,
            "total_tokens": 180,
        }, _merge_child_usage(usage, {
            "input_tokens": -1,
            "prompt_tokens": 140,
            "output_tokens": -2,
            "completion_tokens": 40,
            "total_tokens": 180,
        }))

    async def test_child_uses_large_budget_at_exact_contract_score_boundary(self):
        fields = [f"field_{index}" for index in range(12)]
        schemas = {field: {"type": "object"} for field in fields}
        footer = "RESULT_FIELDS_JSON: " + json.dumps(
            {field: {"value": "bounded"} for field in fields},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        observed: dict[str, int] = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed["max_tokens"] = kwargs["max_tokens"]
            terminal = {
                "type": "agent_event",
                "event_type": "run.completed",
                "run_id": kwargs["run_id"],
                "payload": {"finish_reason": "stop"},
            }
            yield {"type": "delta", "content": footer}
            await kwargs["event_sink"](terminal)
            yield terminal
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/large-contract.json",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "return the exact generic typed fields",
                    "required_result_fields": fields,
                    "required_result_schema": schemas,
                },
                _context(),
                0,
            )

        # 12*8 field points + 25 schema nodes = 121, the first score above
        # the shared 16K tier. This exercises the actual child run boundary,
        # not only the helper in isolation.
        self.assertEqual(32_768, observed["max_tokens"])
        self.assertEqual("completed", result["status"], result)
        self.assertTrue(result["result_field_audit"]["footer_valid"])

    async def test_failed_child_terminal_preserves_usage_and_manifest(self):
        observed_events: list[dict] = []

        async def capture(event):
            observed_events.append(dict(event))

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            terminal = {
                "type": "agent_event",
                "event_type": "run.failed",
                "run_id": kwargs["run_id"],
                "payload": {
                    "error": "bounded provider failure",
                    "finish_reason": "provider_failed",
                    "terminal_reason": "provider_failed",
                    "failure_class": "provider",
                    "retryable": True,
                    "usage": {
                        "input_tokens": -1,
                        "prompt_tokens": 321,
                        "output_tokens": -1,
                        "completion_tokens": 45,
                        "total_tokens": 366,
                    },
                },
            }
            await kwargs["event_sink"](terminal)
            yield terminal
            yield {"type": "done", "finish_reason": "provider_failed"}

        with patch("agent_loop.run_stream", fake_run_stream):
            result = await _run_child(
                {"goal": "return bounded evidence"},
                _context(event_sink=capture),
                0,
            )

        expected_usage = {
            "input_tokens": 321,
            "output_tokens": 45,
            "total_tokens": 366,
        }
        self.assertEqual("error", result["status"])
        self.assertEqual(expected_usage, result["usage"])
        terminal = observed_events[-1]
        self.assertEqual("run.failed", terminal["event_type"])
        self.assertEqual(expected_usage, terminal["payload"]["usage"])
        self.assertEqual(
            result["artifact_manifest"],
            terminal["payload"]["artifact_manifest"],
        )
        self.assertEqual(
            0, terminal["payload"]["artifact_manifest"]["receipt_count"]
        )

    async def test_child_captures_terminal_usage_without_standalone_usage_event(self):
        body = json.dumps({"status": "ok", "evidence": ["bounded"]})

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            terminal = {
                "type": "agent_event",
                "event_type": "run.completed",
                "run_id": kwargs["run_id"],
                "payload": {
                    "finish_reason": "stop",
                    "usage": {
                        "prompt_tokens": 321,
                        "completion_tokens": 45,
                        "total_tokens": 366,
                    },
                },
            }
            yield {"type": "delta", "content": body}
            await kwargs["event_sink"](terminal)
            yield terminal
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/terminal-usage.json",
            ),
        ):
            result = await _run_child(
                {"goal": "return bounded evidence"},
                _context(),
                0,
            )

        self.assertEqual({
            "input_tokens": 321,
            "output_tokens": 45,
            "total_tokens": 366,
        }, result["usage"])

    def test_machine_gap_ledger_is_receipt_owned_and_footer_stays_terminal(self):
        content = (
            "# Findings\n"
            "```text\n"
            "KNOWLEDGE_GATE_GAPS_JSON: documented example\n"
            "```\n"
            "KNOWLEDGE_GATE_GAPS_JSON: {malformed model state}\n"
            "KNOWLEDGE_GATE_GAPS_JSON: "
            '{"status":"degraded","gap_ids":["invented"]}\n'
            'RESULT_FIELDS_JSON: {"finding":"bounded"}'
        )

        canonical, audit = _canonicalize_machine_gap_ledger(
            content,
            "KNOWLEDGE_GATE_GAPS_JSON",
            ["group:source-a:failed", "group:source-a:failed"],
        )

        self.assertIn(
            "KNOWLEDGE_GATE_GAPS_JSON: documented example",
            canonical,
        )
        self.assertNotIn("invented", canonical)
        self.assertNotIn("malformed model state", canonical)
        self.assertEqual(2, audit["removed_model_ledger_count"])
        self.assertTrue(audit["inserted_canonical_ledger"])
        self.assertEqual(
            'RESULT_FIELDS_JSON: {"finding":"bounded"}',
            canonical.splitlines()[-1],
        )
        self.assertIsNone(_exact_knowledge_gate_gap_ledger_error(
            canonical,
            ["group:source-a:failed"],
        ))

    def test_machine_gate_check_ledger_replaces_model_state_from_receipts(self):
        content = (
            "# Findings\n"
            "```text\n"
            "KNOWLEDGE_GATE_CHECKS_JSON: documented example\n"
            "```\n"
            "KNOWLEDGE_GATE_CHECKS_JSON: {forged model state}\n"
            'RESULT_FIELDS_JSON: {"finding":"bounded"}'
        )
        plan = {
            "groups": [
                {"id": "group-a", "check_id": "CHECK-A"},
                {"id": "group-b", "check_id": "CHECK-B"},
            ],
        }
        receipt_audit = {
            "decisions": [
                {"check_id": "CHECK-A", "outcome": "yes"},
                {"check_id": "CHECK-B", "outcome": "no"},
            ],
            "unknown_check_ids": [],
            "failed_group_ids": ["group-b"],
            "unresolved_group_ids": [],
            "missing_receipt_group_ids": [],
        }

        canonical, audit = (
            _canonicalize_machine_knowledge_gate_check_ledger(
                content,
                plan,
                receipt_audit,
            )
        )

        self.assertIn(
            "KNOWLEDGE_GATE_CHECKS_JSON: documented example",
            canonical,
        )
        self.assertNotIn("forged model state", canonical)
        self.assertIn(
            '"id":"CHECK-A","decision":"yes","status":"pass"',
            canonical,
        )
        self.assertIn(
            '"id":"CHECK-B","decision":"no","status":"degraded"',
            canonical,
        )
        self.assertEqual(1, audit["removed_model_ledger_count"])
        self.assertEqual(1, audit["degraded_check_count"])
        self.assertIsNone(
            _completion_quality_declaration(canonical)["status"]
        )
        self.assertEqual(
            'RESULT_FIELDS_JSON: {"finding":"bounded"}',
            canonical.splitlines()[-1],
        )

    def test_machine_gap_ledger_is_removed_when_receipts_have_no_gaps(self):
        canonical, audit = _canonicalize_machine_gap_ledger(
            "Evidence is complete.\n"
            "CAPABILITY_GAPS_JSON: "
            '{"status":"degraded","failed_candidate_ids":["stale"]}',
            "CAPABILITY_GAPS_JSON",
            [],
        )

        self.assertEqual("Evidence is complete.", canonical)
        self.assertEqual(1, audit["removed_model_ledger_count"])
        self.assertFalse(audit["inserted_canonical_ledger"])
        self.assertIsNone(_exact_capability_gap_ledger_error(canonical, []))

    def test_only_explicit_status_shapes_declare_degraded_completion(self):
        self.assertTrue(
            _content_declares_degraded_completion(
                "Status: DEGRADED\nVerified evidence remains reusable."
            )
        )
        self.assertTrue(
            _content_declares_degraded_completion(
                "| source | WARN | endpoint unavailable |"
            )
        )
        self.assertTrue(
            _content_declares_degraded_completion(
                '{"completion_quality":"degraded","evidence":"bounded"}'
            )
        )
        self.assertTrue(
            _content_declares_degraded_completion(
                "完成质量：降级\n已明确隔离证据缺口。"
            )
        )
        self.assertFalse(
            _content_declares_degraded_completion(
                "The completed result is not degraded and contains no warning."
            )
        )
        self.assertFalse(
            _content_declares_degraded_completion(
                "STATUS: NOT DEGRADED\nThe evidence contract is complete."
            )
        )
        self.assertFalse(
            _content_declares_degraded_completion(
                "NO WARNING\nAll declared evidence checks passed."
            )
        )
        self.assertFalse(
            _content_declares_degraded_completion(
                "### Fallback / Degraded Status\n"
                "| Degraded evidence | None |\n"
                "All calls succeeded; no degraded evidence status applies."
            )
        )
        self.assertTrue(
            _content_declares_degraded_completion(
                "- **Degraded status**: YES — no live source receipt"
            )
        )
        self.assertFalse(
            _content_declares_degraded_completion(
                'COMPLETION_QUALITY_JSON: {"status":"complete"}'
            )
        )
        self.assertTrue(
            _content_declares_degraded_completion(
                "COMPLETION_QUALITY_JSON: "
                '{"status":"degraded","reason":"source unavailable"}'
            )
        )
        self.assertFalse(
            _content_declares_degraded_completion(
                "```json\n"
                "COMPLETION_QUALITY_JSON: "
                '{"status":"degraded","reason":"format example"}\n'
                "```"
            )
        )

    def test_completion_quality_machine_ledger_is_strict_and_unscoped_gaps_do_not_win(self):
        complete = _completion_quality_declaration(
            'COMPLETION_QUALITY_JSON: {"status":"complete"}'
        )
        degraded = _completion_quality_declaration(
            "COMPLETION_QUALITY_JSON: "
            '{"status":"degraded","reason":"bounded source gap"}'
        )
        duplicate_key = _completion_quality_declaration(
            "COMPLETION_QUALITY_JSON: "
            '{"status":"complete","status":"degraded"}'
        )
        malformed = _completion_quality_declaration(
            'COMPLETION_QUALITY_JSON: {"status":'
        )
        unscoped_gap_with_complete = _completion_quality_declaration(
            'COMPLETION_QUALITY_JSON: {"status":"complete"}\n'
            "CAPABILITY_GAPS_JSON: "
            '{"status":"degraded","failed_candidate_ids":["candidate-1"]}'
        )
        unscoped_gap_only = _completion_quality_declaration(
            "KNOWLEDGE_GATE_GAPS_JSON: "
            '{"status":"degraded","gap_ids":["check-1"]}'
        )

        self.assertEqual("complete", complete["status"])
        self.assertEqual("completion_quality_json", complete["source"])
        self.assertIsNone(complete["error"])
        self.assertEqual("degraded", degraded["status"])
        self.assertIsNone(degraded["error"])
        self.assertIsNotNone(duplicate_key["error"])
        self.assertIsNotNone(malformed["error"])
        self.assertEqual("complete", unscoped_gap_with_complete["status"])
        self.assertIsNone(unscoped_gap_with_complete["error"])
        self.assertIsNone(unscoped_gap_only["status"])
        self.assertEqual("none", unscoped_gap_only["source"])

    def test_quality_deduplication_ignores_code_and_keeps_malformed_fail_closed(self):
        code_fenced = (
            "```text\n"
            'COMPLETION_QUALITY_JSON: {"status":"degraded","reason":"fake"}\n'
            "```\n"
            'COMPLETION_QUALITY_JSON: {"status":"complete"}\n'
        )
        unchanged, code_audit = (
            _canonicalize_duplicate_completion_quality_ledgers(code_fenced)
        )
        self.assertEqual(code_fenced, unchanged)
        self.assertEqual(1, code_audit["candidate_count"])
        self.assertFalse(code_audit["canonicalized"])

        malformed = (
            'COMPLETION_QUALITY_JSON: {"status":"complete"}\n'
            'COMPLETION_QUALITY_JSON: {"status":}\n'
        )
        unchanged, malformed_audit = (
            _canonicalize_duplicate_completion_quality_ledgers(malformed)
        )
        self.assertEqual(malformed, unchanged)
        self.assertEqual(1, malformed_audit["invalid_candidate_count"])
        self.assertEqual(
            "strict_parser_rejection",
            malformed_audit["resolution"],
        )
        self.assertIsNotNone(
            _completion_quality_declaration(unchanged)["error"]
        )

    def test_completion_quality_reason_is_content_addressed_not_body_fatal(
        self,
    ):
        reason = ("bounded evidence gap; " * 55) + "source unavailable"
        declaration = _completion_quality_declaration(
            "COMPLETION_QUALITY_JSON: "
            + json.dumps({
                "status": "degraded",
                "reason": reason,
            })
        )

        self.assertGreater(len(reason), 1_000)
        self.assertEqual("degraded", declaration["status"])
        self.assertIsNone(declaration["error"])
        self.assertEqual(
            len(reason),
            declaration["reason_receipt"]["chars"],
        )
        self.assertEqual(
            hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            declaration["reason_receipt"]["sha256"],
        )

    def test_exact_gap_ledgers_ignore_examples_and_reject_ambiguous_json(self):
        cases = (
            (
                _exact_capability_gap_ledger_error,
                "CAPABILITY_GAPS_JSON",
                "failed_candidate_ids",
                ["candidate-1"],
            ),
            (
                _exact_knowledge_gate_gap_ledger_error,
                "KNOWLEDGE_GATE_GAPS_JSON",
                "gap_ids",
                ["group:gate-1:failed"],
            ),
        )
        for validator, prefix, identifier_key, expected_ids in cases:
            valid = (
                f'{prefix}: {{"status":"degraded","{identifier_key}":'
                + json.dumps(expected_ids, separators=(",", ":"))
                + "}"
            )
            fenced = "```json\n" + valid + "\n```"
            duplicate_key = (
                f'{prefix}: {{"status":"complete","status":"degraded",'
                f'"{identifier_key}":'
                + json.dumps(expected_ids, separators=(",", ":"))
                + "}"
            )
            nonfinite = (
                f'{prefix}: {{"status":NaN,"{identifier_key}":'
                + json.dumps(expected_ids, separators=(",", ":"))
                + "}"
            )
            oversized_identifier = "x" * 33_000
            oversized = (
                f'{prefix}: {{"status":"degraded","{identifier_key}":'
                + json.dumps(
                    [oversized_identifier],
                    separators=(",", ":"),
                )
                + "}"
            )

            with self.subTest(prefix=prefix, case="valid"):
                self.assertIsNone(validator(valid, expected_ids))
            with self.subTest(prefix=prefix, case="fenced_only"):
                self.assertIn(
                    "exactly one",
                    validator(fenced, expected_ids) or "",
                )
            with self.subTest(prefix=prefix, case="fenced_plus_real"):
                self.assertIsNone(
                    validator(fenced + "\n" + valid, expected_ids)
                )
            with self.subTest(prefix=prefix, case="duplicate_key"):
                self.assertIn(
                    "valid finite JSON",
                    validator(duplicate_key, expected_ids) or "",
                )
            with self.subTest(prefix=prefix, case="multiple_ledgers"):
                self.assertIn(
                    "exactly one",
                    validator(valid + "\n" + valid, expected_ids) or "",
                )
            with self.subTest(prefix=prefix, case="nonfinite"):
                self.assertIn(
                    "valid finite JSON",
                    validator(nonfinite, expected_ids) or "",
                )
            with self.subTest(prefix=prefix, case="oversized"):
                self.assertIn(
                    "bounded size",
                    validator(oversized, [oversized_identifier]) or "",
                )

    def test_parenthesized_raw_tool_call_dialect_is_rejected(self):
        content = (
            "I will persist the result now.\n"
            '<tool_call>write_file(path="result.md", content="unsafe")\n'
            "The file has been persisted."
        )

        audit = _raw_pseudo_tool_protocol_audit(content, [])

        self.assertEqual(1, audit["detected_count"])
        self.assertEqual(["write_file"], audit["unsupported_tool_names"])
        self.assertEqual(
            content.index("<tool_call>"),
            audit["raw_protocol_first_offset"],
        )

    async def test_outer_acceptance_quarantines_provisional_output_until_persisted(self):
        body = json.dumps(
            {"status": "ok", "evidence": "x" * 33_000},
            separators=(",", ":"),
        )
        timeline: list[tuple[str, str]] = []
        observed_events: list[dict] = []
        provisional_terminals: list[dict] = []

        async def capture(event):
            observed_events.append(dict(event))
            timeline.append(("event", str(event.get("event_type") or "")))

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            sink = kwargs["event_sink"]
            started = {
                "type": "agent_event",
                "event_type": "run.started",
                "run_id": kwargs["run_id"],
                "payload": {"source": "delegate"},
            }
            debug = {
                "type": "agent_event",
                "event_type": "debug.fixture.receipt",
                "run_id": kwargs["run_id"],
                "payload": {"receipt": "preserved"},
            }
            artifact = {
                "type": "agent_event",
                "event_type": "artifact.created",
                "run_id": kwargs["run_id"],
                "payload": {"path": "evidence.json", "receipt": "preserved"},
            }
            provisional_delta = {
                "type": "agent_event",
                "event_type": "agent.delta",
                "run_id": kwargs["run_id"],
                "payload": {"content": body},
            }
            provisional_reasoning = {
                "type": "agent_event",
                "event_type": "agent.reasoning_delta",
                "run_id": kwargs["run_id"],
                "payload": {"content": "private reasoning"},
            }
            provisional_terminal = {
                "type": "agent_event",
                "event_type": "run.completed",
                "run_id": kwargs["run_id"],
                "payload": {"finish_reason": "stop"},
            }
            provisional_terminals.append(provisional_terminal)
            for lifecycle in (
                started,
                debug,
                artifact,
                provisional_delta,
                provisional_reasoning,
            ):
                await sink(lifecycle)
                # Real run_stream producers both send and yield agent events.
                yield lifecycle
            yield {"type": "delta", "content": body}
            yield {"type": "reasoning_delta", "content": "private reasoning"}
            await sink(provisional_terminal)
            yield provisional_terminal
            yield {"type": "done", "finish_reason": "stop"}

        def persist(content, *args, **kwargs):
            self.assertEqual(body, content)
            timeline.append(("persist", "results/delegate_transaction.json"))
            return "results/delegate_transaction.json"

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                side_effect=persist,
            ),
            patch("tools.delegation.settings.agent_debug_trace", True),
            patch("agent_loop._append_workspace_debug_event") as debug_append,
        ):
            result = await _run_child(
                {"goal": "return one structured value"},
                _context(event_sink=capture),
                0,
            )

        self.assertEqual("completed", result["status"])
        event_types = [event["event_type"] for event in observed_events]
        self.assertEqual(3, event_types.count("agent.delta"))
        self.assertEqual(0, event_types.count("agent.reasoning_delta"))
        self.assertEqual(1, event_types.count("run.completed"))
        self.assertEqual(0, event_types.count("run.failed"))
        self.assertEqual(1, event_types.count("debug.fixture.receipt"))
        self.assertEqual(1, event_types.count("artifact.created"))
        accepted = [
            event for event in observed_events
            if event["event_type"] == "agent.delta"
        ]
        self.assertEqual(
            body,
            "".join(event["payload"]["content"] for event in accepted),
        )
        self.assertTrue(all(
            event["payload"]["transactional_release"]
            for event in accepted
        ))
        self.assertTrue(all(
            len(event["payload"]["content"]) <= 16_000
            for event in accepted
        ))
        self.assertEqual([0, 1, 2], [
            event["payload"]["chunk_index"] for event in accepted
        ])
        self.assertTrue(all(
            event["payload"]["chunk_count"] == 3 for event in accepted
        ))
        persist_index = timeline.index((
            "persist", "results/delegate_transaction.json"
        ))
        delta_index = timeline.index(("event", "agent.delta"))
        completed_index = timeline.index(("event", "run.completed"))
        self.assertLess(persist_index, delta_index)
        self.assertLess(delta_index, completed_index)
        terminal = observed_events[-1]
        self.assertEqual("run.completed", terminal["event_type"])
        self.assertTrue(terminal["payload"]["authoritative"])
        self.assertFalse(terminal["payload"]["provisional_terminal"])
        self.assertEqual(
            "committed",
            terminal["payload"]["output_transaction"]["status"],
        )
        self.assertEqual(
            result["artifact_manifest"],
            terminal["payload"]["artifact_manifest"],
        )
        self.assertEqual(
            hashlib.sha256(b"[]").hexdigest(),
            terminal["payload"]["artifact_manifest"]["sha256"],
        )
        self.assertEqual(1, len(provisional_terminals))
        self.assertFalse(
            provisional_terminals[0]["payload"]["authoritative"]
        )
        self.assertTrue(
            provisional_terminals[0]["payload"]["provisional_terminal"]
        )
        persisted_parent_lifecycle = [
            call.args[2] for call in debug_append.call_args_list
        ]
        self.assertEqual(
            [event["event_type"] for event in persisted_parent_lifecycle],
            ["agent.spawned", "run.completed"],
        )
        persisted_terminal = persisted_parent_lifecycle[-1]
        self.assertEqual(persisted_terminal["seq"], terminal["seq"])
        self.assertTrue(persisted_terminal["payload"]["authoritative"])
        self.assertFalse(
            persisted_terminal["payload"]["provisional_terminal"]
        )

    async def test_advisory_retrieval_frontier_does_not_degrade_outer_child(self):
        body = json.dumps({
            "status": "ok",
            "evidence": "observed bounded page " * 20,
        })
        observed_events: list[dict] = []
        persisted: dict[str, str] = {}
        gap = {
            "status": "unresolved",
            "source": "harness_http_retrieval_completeness",
            "quality_impact": "advisory",
            "retrieval_completeness_policy": "bounded",
            "coverage_status": "partial",
            "open_chain_count": 1,
            "open_frontier_count": 1,
            "open_reasons": {"pagination_more_available": 1},
        }

        async def capture(event):
            observed_events.append(dict(event))

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            sink = kwargs["event_sink"]
            terminal = {
                "type": "agent_event",
                "event_type": "run.completed",
                "run_id": kwargs["run_id"],
                "payload": {
                    "finish_reason": "stop",
                    "unresolved_retrieval": gap,
                },
            }
            yield {"type": "delta", "content": body}
            await sink(terminal)
            yield terminal
            yield {"type": "done", "finish_reason": "stop"}

        def persist(content, *args, **kwargs):
            persisted["content"] = content
            return "results/delegate_advisory_frontier.json"

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                side_effect=persist,
            ),
        ):
            result = await _run_child(
                {"goal": "summarize the bounded evidence page"},
                _context(event_sink=capture),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("complete", result["completion_quality"])
        self.assertEqual(
            "advisory",
            result["unresolved_retrieval"]["quality_impact"],
        )
        audit = result["completion_quality_audit"]
        self.assertNotIn(
            "runtime_unresolved_retrieval",
            audit["receipt_degraded_reasons"],
        )
        self.assertFalse(audit["machine_degraded_evidence"])
        self.assertIn(
            "Coverage: bounded HTTP acquisition",
            persisted["content"],
        )
        self.assertNotIn("WARN/degraded", persisted["content"])
        terminal = observed_events[-1]
        self.assertEqual("run.completed", terminal["event_type"])
        self.assertEqual("complete", terminal["payload"]["completion_quality"])
        self.assertEqual(
            "advisory",
            terminal["payload"]["unresolved_retrieval"]["quality_impact"],
        )

    async def test_long_completion_reason_preserves_substantive_body(self):
        substantive = "# Evidence report\n\n" + (
            "Verified bounded evidence paragraph.\n" * 5_000
        )
        reason = ("bounded evidence gap; " * 55) + "source unavailable"
        footer = (
            "COMPLETION_QUALITY_JSON: "
            + json.dumps({
                "status": "degraded",
                "reason": reason,
            })
        )
        body = substantive + footer
        persisted: dict[str, str] = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {"type": "delta", "content": body}
            yield {"type": "done", "finish_reason": "stop"}

        def persist(content, *args, **kwargs):
            persisted["content"] = content
            return "results/long-completion-reason.md"

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                side_effect=persist,
            ),
        ):
            result = await _run_child(
                {"goal": "return one bounded evidence report"},
                _context(),
                0,
            )

        self.assertGreater(len(reason), 1_000)
        self.assertEqual("completed", result["status"])
        self.assertEqual("degraded", result["completion_quality"])
        self.assertEqual(body, persisted["content"])
        self.assertTrue(persisted["content"].startswith(substantive))
        self.assertEqual(
            hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            result["completion_quality_audit"][
                "declaration_reason_receipt"
            ]["sha256"],
        )

    async def test_outer_validation_failure_discards_provisional_output(self):
        observed_events: list[dict] = []

        async def capture(event):
            observed_events.append(dict(event))

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            sink = kwargs["event_sink"]
            body = (
                "Let me search the first database. I will query another endpoint. "
                "Next I need to inspect returned pages before I prepare a result. "
            ) * 5
            delta = {
                "type": "agent_event",
                "event_type": "agent.delta",
                "run_id": kwargs["run_id"],
                "payload": {"content": body},
            }
            terminal = {
                "type": "agent_event",
                "event_type": "run.completed",
                "run_id": kwargs["run_id"],
                "payload": {"finish_reason": "stop"},
            }
            await sink(delta)
            yield delta
            yield {"type": "delta", "content": body}
            await sink(terminal)
            yield terminal
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch("tools.delegation.persist_result_for_history") as persist,
        ):
            result = await _run_child(
                {"goal": "return final evidence"},
                _context(event_sink=capture),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertIn("process narration", result["error"])
        persist.assert_not_called()
        event_types = [event["event_type"] for event in observed_events]
        self.assertEqual(0, event_types.count("agent.delta"))
        self.assertEqual(0, event_types.count("agent.reasoning_delta"))
        self.assertEqual(0, event_types.count("run.completed"))
        self.assertEqual(1, event_types.count("run.failed"))
        self.assertEqual("run.failed", observed_events[-1]["event_type"])
        self.assertTrue(observed_events[-1]["payload"]["authoritative"])
        self.assertFalse(
            observed_events[-1]["payload"]["provisional_terminal"]
        )
        self.assertEqual(
            "discarded",
            observed_events[-1]["payload"]["output_transaction"]["status"],
        )

    async def test_persistence_failure_discards_provisional_output(self):
        body = '{"status":"ok","items":[1]}'
        observed_events: list[dict] = []

        async def capture(event):
            observed_events.append(dict(event))

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            sink = kwargs["event_sink"]
            delta = {
                "type": "agent_event",
                "event_type": "agent.delta",
                "payload": {"content": body},
            }
            terminal = {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {"finish_reason": "stop"},
            }
            await sink(delta)
            yield delta
            yield {"type": "delta", "content": body}
            await sink(terminal)
            yield terminal
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="debug/not-reusable.json",
            ),
        ):
            result = await _run_child(
                {"goal": "return one structured value"},
                _context(event_sink=capture),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertIn("could not be persisted", result["error"])
        event_types = [event["event_type"] for event in observed_events]
        self.assertEqual(0, event_types.count("agent.delta"))
        self.assertEqual(0, event_types.count("run.completed"))
        self.assertEqual(1, event_types.count("run.failed"))
        self.assertEqual("run.failed", observed_events[-1]["event_type"])
        self.assertTrue(observed_events[-1]["payload"]["authoritative"])
        self.assertFalse(
            observed_events[-1]["payload"]["provisional_terminal"]
        )

    async def test_machine_complete_length_failure_commits_once_as_warning(self):
        body = '{"status":"ok","items":[1]}'
        observed_events: list[dict] = []

        async def capture(event):
            observed_events.append(dict(event))

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            sink = kwargs["event_sink"]
            delta = {
                "type": "agent_event",
                "event_type": "agent.delta",
                "payload": {"content": body},
            }
            failed = {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Delegated model hit its output limit",
                    "finish_reason": "length",
                    "terminal_reason": "model_hit_max_output_tokens",
                    "failure_class": "resource_exhausted",
                    "retryable": False,
                },
            }
            await sink(delta)
            yield delta
            yield {"type": "delta", "content": body}
            await sink(failed)
            yield failed
            yield {
                "type": "error",
                "msg": "Delegated model hit its output limit",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_length_warning.json",
            ),
        ):
            result = await _run_child(
                {"goal": "return one structured value"},
                _context(event_sink=capture),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertIn("output limit", result["runtime_warning"])
        event_types = [event["event_type"] for event in observed_events]
        self.assertEqual(1, event_types.count("agent.delta"))
        self.assertEqual(0, event_types.count("run.failed"))
        self.assertEqual(1, event_types.count("run.completed"))
        self.assertEqual("run.completed", observed_events[-1]["event_type"])
        self.assertTrue(observed_events[-1]["payload"]["authoritative"])
        self.assertFalse(
            observed_events[-1]["payload"]["provisional_terminal"]
        )
        self.assertIn("output limit", observed_events[-1]["payload"][
            "runtime_warning"
        ])

    def test_footer_observability_distinguishes_not_required_from_valid(self):
        not_required = _result_field_audit(
            "ordinary untyped delegated result",
            [],
            {},
        )
        missing = _result_field_audit("no ledger", ["value"], {})
        valid = _result_field_audit(
            'RESULT_FIELDS_JSON: {"value":{"status":"present",'
            '"value_summary":"7","provenance":"calculator"}}',
            ["value"],
            {},
        )

        self.assertTrue(not_required["footer_valid"])
        self.assertFalse(not_required["footer_required"])
        self.assertFalse(not_required["footer_present"])
        self.assertEqual("not_required", not_required["footer_status"])
        self.assertTrue(missing["footer_required"])
        self.assertFalse(missing["footer_present"])
        self.assertEqual("invalid", missing["footer_status"])
        self.assertTrue(valid["footer_present"])
        self.assertEqual("valid", valid["footer_status"])

    def test_missing_schema_defaults_each_field_to_native_unconstrained_json(self):
        fields = ["scalar", "items", "metadata", "unknown"]
        expected_schema = {field: {} for field in fields}
        normalized, error = _strict_result_field_schema(
            {"required_result_fields": fields},
            fields,
        )
        body = "RESULT_FIELDS_JSON: " + json.dumps({
            "scalar": 7.5,
            "items": [1, "two", False],
            "metadata": {"nested": {"ok": True}},
            "unknown": None,
        })

        self.assertIsNone(error)
        self.assertEqual(expected_schema, normalized)
        for schema in (None, normalized):
            audit = _result_field_audit(body, fields, schema)
            self.assertTrue(audit["footer_valid"])
            self.assertEqual(fields, audit["present"])
            self.assertEqual([], audit["missing"])

    def test_default_native_schema_keeps_valid_legacy_envelopes_compatible(self):
        fields = ["available", "unavailable"]
        audit = _result_field_audit(
            "RESULT_FIELDS_JSON: " + json.dumps({
                "available": {
                    "status": "present",
                    "value_summary": "bounded value",
                    "provenance": "retained source",
                },
                "unavailable": {
                    "status": "degraded",
                    "reason": "source unavailable",
                    "provenance": "attempted source/fallback",
                },
            }),
            fields,
            {field: {} for field in fields},
        )

        self.assertTrue(audit["footer_valid"])
        self.assertEqual(fields, audit["present"])
        self.assertEqual([], audit["degraded"])

    def test_default_native_schema_still_requires_exact_footer_keys(self):
        fields = ["left", "right"]
        schema = {field: {} for field in fields}
        missing = _result_field_audit(
            'RESULT_FIELDS_JSON: {"left":1}',
            fields,
            schema,
        )
        extra = _result_field_audit(
            'RESULT_FIELDS_JSON: {"left":1,"right":2,"extra":3}',
            fields,
            schema,
        )

        self.assertFalse(missing["footer_valid"])
        self.assertFalse(extra["footer_valid"])
        self.assertIn("keys must exactly match", missing["footer_error"])
        self.assertIn("keys must exactly match", extra["footer_error"])

    def test_native_skill_result_schema_preserves_number_and_array_values(self):
        fields = ["sum", "rows"]
        schema = {
            "sum": {"type": "number"},
            "rows": {"type": "array", "items": {"type": "integer"}},
        }
        valid = _result_field_audit(
            'RESULT_FIELDS_JSON: {"sum":7.5,"rows":[1,2,3]}',
            fields,
            schema,
        )
        wrapped = _result_field_audit(
            'RESULT_FIELDS_JSON: {"sum":{"status":"present",'
            '"value_summary":"7.5","provenance":"calc"},"rows":[]}',
            fields,
            schema,
        )

        self.assertTrue(valid["footer_valid"])
        self.assertEqual(fields, valid["present"])
        self.assertFalse(wrapped["footer_valid"])
        self.assertIn("sum", wrapped["missing"])

    async def test_native_schema_is_forwarded_to_inner_and_outer_contract(self):
        fields = ["sum", "rows"]
        schema = {
            "sum": {"type": "number"},
            "rows": {"type": "array", "items": {"type": "integer"}},
        }
        observed: dict[str, object] = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed["required_result_fields"] = kwargs.get(
                "required_result_fields"
            )
            observed["required_result_schema"] = kwargs.get(
                "required_result_schema"
            )
            yield {
                "type": "delta",
                "content": (
                    "RESULT_FIELDS_JSON: "
                    + json.dumps({"sum": 7.5, "rows": [1, 2, 3]})
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/native.json",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "return schema-native values",
                    "required_result_fields": fields,
                    "required_result_schema": schema,
                },
                _context(),
                0,
            )

        self.assertEqual(fields, observed["required_result_fields"])
        self.assertEqual(schema, observed["required_result_schema"])
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["result_field_audit"]["footer_valid"])
        self.assertEqual(fields, result["result_field_audit"]["present"])

    async def test_omitted_schema_forwards_native_default_and_uses_native_prompt(self):
        fields = ["count", "rows", "metadata", "optional"]
        expected_schema = {field: {} for field in fields}
        observed: dict[str, object] = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed["prompt"] = str(messages[0]["content"])
            observed["required_result_schema"] = kwargs.get(
                "required_result_schema"
            )
            yield {
                "type": "delta",
                "content": "RESULT_FIELDS_JSON: " + json.dumps({
                    "count": 3,
                    "rows": [1, 2, 3],
                    "metadata": {"source": "fixture"},
                    "optional": None,
                }),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/native-default.json",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "return native typed values",
                    "required_result_fields": fields,
                },
                _context(),
                0,
            )

        prompt = str(observed["prompt"])
        self.assertEqual(expected_schema, observed["required_result_schema"])
        self.assertEqual(expected_schema, result["required_result_schema"])
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["result_field_audit"]["footer_valid"])
        self.assertIn("Preserve raw numbers, strings, arrays, and objects", prompt)
        self.assertNotIn("value_summary", prompt)
        self.assertNotIn("Each value must be an object", prompt)
        self.assertNotIn("DEGRADED GAP", prompt)

    def test_explicit_status_envelope_schema_retains_envelope_semantics(self):
        envelope = _legacy_envelope_schema()
        audit = _result_field_audit(
            'RESULT_FIELDS_JSON: {"value":{"status":"present",'
            '"value_summary":"7","provenance":"calculator"}}',
            ["value"],
            {"value": envelope},
        )
        invalid = _result_field_audit(
            'RESULT_FIELDS_JSON: {"value":{"status":"degraded",'
            '"reason":"source unavailable","provenance":""}}',
            ["value"],
            {"value": envelope},
        )

        self.assertTrue(audit["footer_valid"])
        self.assertEqual(["value"], audit["present"])
        self.assertFalse(invalid["footer_valid"])
        self.assertEqual(["value"], invalid["missing"])

    def test_native_result_schema_uses_registry_constraints_and_rejects_malformed(self):
        schema = {
            "sum": {"type": "number", "minimum": 10},
            "rows": {
                "type": "array",
                "minItems": 2,
                "items": {"type": "string", "minLength": 2},
            },
        }
        invalid = _result_field_audit(
            'RESULT_FIELDS_JSON: {"sum":7,"rows":["x"]}',
            ["sum", "rows"],
            schema,
        )
        valid = _result_field_audit(
            'RESULT_FIELDS_JSON: {"sum":10,"rows":["aa","bb"]}',
            ["sum", "rows"],
            schema,
        )
        normalized, error = _strict_result_field_schema(
            {
                "required_result_fields": ["rows"],
                "required_result_schema": {
                    "rows": {"type": "array", "minItems": -1},
                },
            },
            ["rows"],
        )

        self.assertFalse(invalid["footer_valid"])
        self.assertEqual(["sum", "rows"], invalid["missing"])
        self.assertTrue(valid["footer_valid"])
        self.assertEqual({}, normalized)
        self.assertIn("minItems", error or "")

    def test_footer_tail_strip_ignores_markdown_code_examples(self):
        example = (
            "# Format documentation\n"
            "```json\n"
            "RESULT_FIELDS_JSON: {\"example\": true}\n"
            "```\n"
            "    RESULT_FIELDS_JSON: {\"indented\": true}\n"
            "Substantive documentation after both examples.\n"
        )
        retained, removed = strip_result_fields_candidate_tail(example)
        self.assertEqual(example, retained)
        self.assertEqual(0, removed)

        malformed_terminal = example + "RESULT_FIELDS_JSON: {\n  \"field\":\n"
        retained, removed = strip_result_fields_candidate_tail(
            malformed_terminal
        )
        self.assertEqual(example, retained)
        self.assertGreater(removed, 0)

        multiple_stale = (
            example
            + "RESULT_FIELDS_JSON: {\"first\": true}\n"
            + "stale prose that cannot follow a terminal ledger\n"
            + "RESULT_FIELDS_JSON: {\"second\": true}\n"
        )
        retained, removed = strip_result_fields_candidate_tail(multiple_stale)
        self.assertEqual(example, retained)
        self.assertEqual(len(multiple_stale) - len(example), removed)

    def test_typed_footer_rejects_multiple_non_code_candidates(self):
        footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "field": {
                "status": "present",
                "value_summary": "bounded",
                "provenance": "source",
            },
        })
        multiple = "RESULT_FIELDS_JSON: {\n" + footer
        audit = _result_field_audit(multiple, ["field"])
        self.assertFalse(audit["footer_valid"])
        self.assertIn("multiple non-code", audit["footer_error"])

        documented = (
            "```json\nRESULT_FIELDS_JSON: {\"example\": true}\n```\n"
            + footer
        )
        audit = _result_field_audit(documented, ["field"])
        self.assertTrue(audit["footer_valid"])

        for hidden in (
            "```json\n" + footer,
            "    " + footer,
            "\t" + footer,
        ):
            audit = _result_field_audit(hidden, ["field"])
            self.assertFalse(audit["footer_valid"])
            self.assertIn("no protocol-visible", audit["footer_error"])

    def test_strict_footer_canonicalizer_repairs_framing_only(self):
        pretty = (
            "Report body\nRESULT_FIELDS_JSON: {\n"
            '  "rows": [1, 2],\n'
            '  "count": 2\n'
            "}\n"
        )
        recovered = extract_canonical_result_fields_footer(
            pretty,
            ["rows", "count"],
            {
                "rows": {"type": "array", "items": {"type": "integer"}},
                "count": {"type": "integer"},
            },
        )

        self.assertTrue(recovered["recovered"])
        self.assertEqual(
            'RESULT_FIELDS_JSON: {"rows":[1,2],"count":2}',
            recovered["footer"],
        )

    def test_strict_footer_canonicalizer_rejects_ambiguous_candidates(self):
        for content in (
            'RESULT_FIELDS_JSON: {"field":1}\ntrailing prose',
            (
                'RESULT_FIELDS_JSON: {"field":1}\n'
                'RESULT_FIELDS_JSON: {"field":1}'
            ),
            'RESULT_FIELDS_JSON: {"field":',
        ):
            recovered = extract_canonical_result_fields_footer(
                content,
                ["field"],
                {"field": {"type": "integer"}},
            )
            self.assertFalse(recovered["recovered"], content)
            self.assertNotIn(str(content), str({
                key: value
                for key, value in recovered.items()
                if key != "footer"
            }))

    def test_authoritative_footer_and_internal_submit_reject_nonfinite_json(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            raw = '{"field":' + constant + "}"
            with self.subTest(constant=constant, boundary="terminal_footer"):
                audit = _result_field_audit(
                    "RESULT_FIELDS_JSON: " + raw,
                    ["field"],
                )
                self.assertFalse(audit["footer_valid"])
                self.assertIn("invalid", audit["footer_error"])

            with self.subTest(constant=constant, boundary="internal_submit"):
                footer, audit = canonical_result_fields_footer_from_json(
                    raw,
                    ["field"],
                    {"field": {}},
                )
                self.assertIsNone(footer)
                self.assertFalse(audit["footer_valid"])

    def test_internal_submitter_unwraps_only_schema_proven_json_containers(self):
        schema = {
            "mission_manifest": {
                "type": "object",
                "properties": {
                    "vehicle": {"type": "string"},
                    "revision": {"type": "integer"},
                },
                "required": ["vehicle", "revision"],
                "additionalProperties": False,
            },
            "observations": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
            },
        }
        values = {
            "mission_manifest": json.dumps({
                "vehicle": "orbiter",
                "revision": 3,
            }),
            "observations": json.dumps([1.25, 2.5]),
        }

        footer, audit, diagnostics = (
            canonical_result_fields_footer_from_internal_submitter_json(
                json.dumps(values),
                ["mission_manifest", "observations"],
                schema,
            )
        )

        self.assertTrue(audit["footer_valid"])
        self.assertIsNotNone(footer)
        self.assertEqual({
            "mission_manifest": {
                "vehicle": "orbiter",
                "revision": 3,
            },
            "observations": [1.25, 2.5],
        }, json.loads(footer.split(": ", 1)[1]))
        self.assertTrue(diagnostics["transport_envelope_normalized"])
        self.assertEqual(2, diagnostics["normalized_field_count"])
        self.assertEqual(2, len(diagnostics["normalized_field_name_sha256"]))
        self.assertNotIn("orbiter", json.dumps(diagnostics))

    def test_internal_submitter_never_reinterprets_schema_valid_strings(self):
        value = '{"note":"this is intentionally text"}'
        schema = {
            "payload": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "object"},
                ],
            },
        }

        footer, audit, diagnostics = (
            canonical_result_fields_footer_from_internal_submitter_json(
                json.dumps({"payload": value}),
                ["payload"],
                schema,
            )
        )

        self.assertTrue(audit["footer_valid"])
        self.assertEqual(
            {"payload": value},
            json.loads(footer.split(": ", 1)[1]),
        )
        self.assertFalse(diagnostics["transport_envelope_normalized"])
        self.assertEqual(1, diagnostics["rejected_candidate_count"])

    def test_internal_submitter_rejects_ambiguous_or_invalid_envelopes(self):
        schema = {
            "manifest": {
                "type": "object",
                "properties": {"revision": {"type": "integer"}},
                "required": ["revision"],
                "additionalProperties": False,
            },
        }
        invalid_values = (
            '{"revision":3} trailing',
            '{"revision":3,"revision":4}',
            '{"revision":NaN}',
            '[3]',
            '{"revision":"three"}',
            '"scalar"',
            'ordinary prose',
        )
        for value in invalid_values:
            with self.subTest(value=value):
                footer, audit, diagnostics = (
                    canonical_result_fields_footer_from_internal_submitter_json(
                        json.dumps({"manifest": value}),
                        ["manifest"],
                        schema,
                    )
                )
                self.assertIsNone(footer)
                self.assertFalse(audit["footer_valid"])
                self.assertFalse(
                    diagnostics["transport_envelope_normalized"]
                )

    def test_regular_footer_parser_remains_strict_about_stringified_objects(self):
        schema = {"manifest": {"type": "object"}}
        raw = json.dumps({"manifest": json.dumps({"revision": 3})})

        footer, audit = canonical_result_fields_footer_from_json(
            raw,
            ["manifest"],
            schema,
        )

        self.assertIsNone(footer)
        self.assertFalse(audit["footer_valid"])

    def test_schema_exposes_typed_result_fields_for_single_and_batch_tasks(self):
        properties = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("required_result_fields", properties)
        self.assertIn("required_result_schema", properties)
        task_properties = properties["tasks"]["items"]["properties"]
        self.assertIn("required_result_fields", task_properties)
        self.assertIn("required_result_schema", task_properties)

    def test_ascii_result_field_names_do_not_match_larger_words(self):
        audit = _result_field_audit(
            "The cohort was followed for five years.",
            ["year"],
        )
        self.assertEqual(audit["missing"], ["year"])
        self.assertFalse(audit["footer_valid"])

    def test_prose_field_mentions_do_not_satisfy_typed_ledger(self):
        audit = _result_field_audit(
            "study_title, enrollment_count, and provenance are all covered.",
            ["study_title", "enrollment_count"],
        )
        self.assertFalse(audit["footer_valid"])
        self.assertEqual(
            audit["missing"],
            ["study_title", "enrollment_count"],
        )

    async def test_malformed_result_field_metadata_fails_before_model(self):
        with (
            patch("agent_loop.run_stream") as run_stream,
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "extract typed evidence",
                    "required_result_fields": "title, enrollment",
                },
                _context(),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_class"], "contract_validation")
        self.assertIn("explicit list", result["error"])
        run_stream.assert_not_called()
        persist_result.assert_not_called()

    async def test_exact_fields_and_explicit_degraded_gap_complete_contract(self):
        observed: dict[str, object] = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed["prompt"] = str(messages[0]["content"])
            observed["required_result_fields"] = kwargs.get(
                "required_result_fields"
            )
            ledger = {
                "study_title": {
                    "status": "present",
                    "value_summary": "A verified registry record",
                    "provenance": "registry record",
                },
                "enrollment_count": {
                    "status": "degraded",
                    "reason": "registry endpoint unavailable",
                    "provenance": "registry attempt and fallback audit",
                },
            }
            yield {
                "type": "delta",
                "content": (
                    "# Findings\n"
                    "- study_title: A verified registry record with stable provenance.\n"
                    "- enrollment_count — DEGRADED GAP: registry endpoint was unavailable; "
                    "no value was fabricated.\n"
                    "# Verification\n"
                    "The title is supported by the persisted source record. The unavailable "
                    "enrollment value is explicitly isolated from verified facts. " * 3
                    + "\nRESULT_FIELDS_JSON: "
                    + json.dumps(ledger, ensure_ascii=False, separators=(",", ":"))
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_typed.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "extract typed evidence",
                    "required_result_fields": [
                        "study_title",
                        "enrollment_count",
                    ],
                },
                _context(),
                0,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completion_quality"], "degraded")
        self.assertEqual(result["result_path"], "results/delegate_typed.md")
        persist_result.assert_called_once()
        self.assertEqual(result["required_result_fields"], [
            "study_title",
            "enrollment_count",
        ])
        self.assertEqual(
            result["result_field_audit"]["present"],
            ["study_title", "enrollment_count"],
        )
        self.assertEqual(result["result_field_audit"]["degraded"], [])
        self.assertEqual(result["result_field_audit"]["missing"], [])
        self.assertIn("Required typed result fields", observed["prompt"])
        self.assertNotIn("DEGRADED GAP", observed["prompt"])
        self.assertNotIn("value_summary", observed["prompt"])
        self.assertIn("RESULT_FIELDS_JSON", observed["prompt"])
        self.assertEqual(
            observed["required_result_fields"],
            ["study_title", "enrollment_count"],
        )

    async def test_short_typed_result_is_judged_by_contract_not_global_length(self):
        ledger = {
            "status": {
                "status": "degraded",
                "reason": "No source",
                "provenance": "source attempt",
            },
        }
        body = "RESULT_FIELDS_JSON: " + json.dumps(
            ledger,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assertLess(len(body), 200)

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {"type": "delta", "content": body}
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_short_typed.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "return one typed status",
                    "required_result_fields": ["status"],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertTrue(result["result_field_audit"]["footer_valid"])

    async def test_populated_typed_result_without_evidence_receipt_fails_contract(self):
        observed: dict[str, object] = {}
        skill_body = (
            "# Catalog database\n"
            "Use the declared database query capability for live evidence."
        )
        skill_bytes = skill_body.encode("utf-8")

        async def fake_preload(tool_args, *, context, progress=None):
            return (
                {"success": True, "content": skill_body},
                {
                    "page_count": 1,
                    "total_chars": len(skill_body),
                    "total_bytes": len(skill_bytes),
                    "complete": True,
                    "sha256": hashlib.sha256(skill_bytes).hexdigest(),
                },
            )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed["prompt"] = str(messages[0]["content"])
            yield {
                "type": "delta",
                "content": (
                    "# Result\n"
                    "A typed value was supplied without a live query receipt.\n"
                    'COMPLETION_QUALITY_JSON: {"status":"complete"}\n'
                    'RESULT_FIELDS_JSON: {"record_id":"DB-1"}'
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch(
                "tools.delegation._load_complete_skill_view_preload",
                fake_preload,
            ),
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/unverified_typed_evidence.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "query one catalog record",
                    "step_type": "knowledge_bootstrap",
                    "tools": ["skill_view"],
                    "required_result_fields": ["record_id"],
                    "required_result_schema": {
                        "record_id": {"type": "string"},
                    },
                    "required_capability_skills": ["catalog-database"],
                },
                _context("skill_view"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertIsNone(result["completion_quality"])
        self.assertIn(
            "COMPLETION_QUALITY_JSON declares complete",
            result["error"],
        )
        self.assertIn(
            "no_verified_evidence_dispatch_receipt",
            result["error"],
        )
        self.assertTrue(result["retryable"])
        persist_result.assert_not_called()
        audit = result["completion_quality_audit"]
        self.assertEqual("complete", audit["declared_status"])
        self.assertTrue(audit["receipt_forced_degraded"])
        self.assertTrue(audit["unverified_typed_evidence"])
        self.assertEqual(0, audit["attempted_evidence_receipt_count"])
        self.assertEqual(0, audit["successful_evidence_receipt_count"])
        self.assertIn(
            "no_verified_evidence_dispatch_receipt",
            audit["receipt_degraded_reasons"],
        )
        self.assertEqual(
            ["record_id"],
            audit["unverified_typed_value_fields"],
        )
        self.assertIn("COMPLETION_QUALITY_JSON", str(observed["prompt"]))

    async def test_nullable_typed_gap_without_receipt_completes_degraded(self):
        skill_body = (
            "# Catalog database\n"
            "Use the declared database query capability for live evidence."
        )
        skill_bytes = skill_body.encode("utf-8")

        async def fake_preload(tool_args, *, context, progress=None):
            return (
                {"success": True, "content": skill_body},
                {
                    "page_count": 1,
                    "total_chars": len(skill_body),
                    "total_bytes": len(skill_bytes),
                    "complete": True,
                    "sha256": hashlib.sha256(skill_bytes).hexdigest(),
                },
            )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Evidence gap\n"
                    "No live database receipt was obtained; no identifier was "
                    "asserted.\n"
                    "COMPLETION_QUALITY_JSON: "
                    '{"status":"degraded","reason":"live source unavailable"}\n'
                    'RESULT_FIELDS_JSON: {"record_id":null}'
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch(
                "tools.delegation._load_complete_skill_view_preload",
                fake_preload,
            ),
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/verified_null_gap.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "query one catalog record",
                    "step_type": "knowledge_bootstrap",
                    "tools": ["skill_view"],
                    "required_result_fields": ["record_id"],
                    "required_result_schema": {
                        "record_id": {"type": ["string", "null"]},
                    },
                    "required_capability_skills": ["catalog-database"],
                },
                _context("skill_view"),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("degraded", result["completion_quality"])
        persist_result.assert_called_once()
        audit = result["completion_quality_audit"]
        self.assertEqual(["record_id"], audit["typed_null_gap_fields"])
        self.assertEqual([], audit["unverified_typed_value_fields"])
        self.assertTrue(audit["machine_degraded_evidence"])

    async def test_duplicate_valid_quality_ledgers_are_canonicalized_worst_first(self):
        """A repair append must not spend the parent retry on duplicate control prose."""

        skill_body = "# Inventory catalog\nUse live evidence receipts."
        skill_bytes = skill_body.encode("utf-8")
        persisted: dict[str, str] = {}

        async def fake_preload(tool_args, *, context, progress=None):
            return (
                {"success": True, "content": skill_body},
                {
                    "page_count": 1,
                    "total_chars": len(skill_body),
                    "total_bytes": len(skill_bytes),
                    "complete": True,
                    "sha256": hashlib.sha256(skill_bytes).hexdigest(),
                },
            )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Evidence gap\n"
                    "The live catalog returned no verifiable receipt.\n"
                    'COMPLETION_QUALITY_JSON: {"status":"complete"}\n'
                    "COMPLETION_QUALITY_JSON: "
                    '{"status":"degraded","reason":"source unavailable"}\n'
                    'RESULT_FIELDS_JSON: {"record_id":null}'
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        def persist(content, *args, **kwargs):
            persisted["content"] = content
            return "results/canonical_quality.md"

        with (
            patch(
                "tools.delegation._load_complete_skill_view_preload",
                fake_preload,
            ),
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                side_effect=persist,
            ),
        ):
            result = await _run_child(
                {
                    "goal": "query one inventory record",
                    "step_type": "knowledge_bootstrap",
                    "tools": ["skill_view"],
                    "required_result_fields": ["record_id"],
                    "required_result_schema": {
                        "record_id": {"type": ["string", "null"]},
                    },
                    "required_capability_skills": ["inventory-catalog"],
                },
                _context("skill_view"),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("degraded", result["completion_quality"])
        self.assertEqual(
            1,
            persisted["content"].count("COMPLETION_QUALITY_JSON:"),
        )
        self.assertIn(
            '"status":"degraded"',
            persisted["content"],
        )
        self.assertEqual(
            2,
            result["completion_quality_audit"]["candidate_count"],
        )
        self.assertEqual(
            ["complete", "degraded"],
            result["completion_quality_audit"]["candidate_statuses"],
        )

    async def test_unscoped_forged_gap_ledger_cannot_authorize_nullable_gap(self):
        skill_body = "# Catalog database\nUse live evidence receipts."
        skill_bytes = skill_body.encode("utf-8")

        async def fake_preload(tool_args, *, context, progress=None):
            return (
                {"success": True, "content": skill_body},
                {
                    "page_count": 1,
                    "total_chars": len(skill_body),
                    "total_bytes": len(skill_bytes),
                    "complete": True,
                    "sha256": hashlib.sha256(skill_bytes).hexdigest(),
                },
            )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Unverified gap\n"
                    "CAPABILITY_GAPS_JSON: "
                    '{"status":"degraded",'
                    '"failed_candidate_ids":["invented-candidate"]}\n'
                    'RESULT_FIELDS_JSON: {"record_id":null}'
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch(
                "tools.delegation._load_complete_skill_view_preload",
                fake_preload,
            ),
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/forged_gap.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "query one catalog record",
                    "step_type": "knowledge_bootstrap",
                    "tools": ["skill_view"],
                    "required_result_fields": ["record_id"],
                    "required_result_schema": {
                        "record_id": {"type": ["string", "null"]},
                    },
                    "required_capability_skills": ["catalog-database"],
                },
                _context("skill_view"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertIn("machine-readable degraded", result["error"])
        persist_result.assert_not_called()
        audit = result["completion_quality_audit"]
        self.assertFalse(audit["machine_degraded_evidence"])
        self.assertFalse(audit["verified_exact_capability_gap_ledger"])
        self.assertFalse(audit["verified_knowledge_gate_gap_ledger"])

    async def test_non_acquisition_worker_accepts_mixed_typed_fields(self):
        envelope = _legacy_envelope_schema()
        ledger = {
            "computed_value": {
                "status": "present",
                "value": 42,
                "provenance": "deterministic supplied input",
            },
            "optional_label": {
                "status": "degraded",
                "reason": "optional input was not supplied",
                "provenance": "worker input contract",
            },
        }

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Worker result\n"
                    "COMPLETION_QUALITY_JSON: "
                    '{"status":"degraded","reason":"optional input absent"}\n'
                    "RESULT_FIELDS_JSON: "
                    + json.dumps(ledger, separators=(",", ":"))
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/mixed_worker_fields.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "compute one value and classify an optional label",
                    "step_type": "worker",
                    "required_result_fields": [
                        "computed_value",
                        "optional_label",
                    ],
                    "required_result_schema": {
                        "computed_value": envelope,
                        "optional_label": envelope,
                    },
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("degraded", result["completion_quality"])
        persist_result.assert_called_once()
        self.assertEqual(
            ["computed_value"],
            result["result_field_audit"]["present"],
        )
        self.assertEqual(
            ["optional_label"],
            result["result_field_audit"]["degraded"],
        )
        audit = result["completion_quality_audit"]
        self.assertFalse(audit["evidence_acquisition_step"])
        self.assertFalse(audit["no_verified_value_source"])

    async def test_machine_complete_conflicts_with_typed_degraded_field(self):
        envelope = _legacy_envelope_schema()
        ledger = {
            "optional_label": {
                "status": "degraded",
                "reason": "optional input was not supplied",
                "provenance": "worker input contract",
            },
        }

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Contradictory worker result\n"
                    'COMPLETION_QUALITY_JSON: {"status":"complete"}\n'
                    "RESULT_FIELDS_JSON: "
                    + json.dumps(ledger, separators=(",", ":"))
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/contradictory_quality.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "classify one optional label",
                    "step_type": "worker",
                    "required_result_fields": ["optional_label"],
                    "required_result_schema": {
                        "optional_label": envelope,
                    },
                },
                _context(),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertIn(
            "declares complete while Harness-owned",
            result["error"],
        )
        self.assertTrue(result["retryable"])
        persist_result.assert_not_called()
        self.assertEqual(
            ["typed_result_field_gap"],
            result["completion_quality_audit"][
                "strict_complete_conflict_reasons"
            ],
        )

    async def test_partial_acquisition_with_success_receipt_accepts_mixed_fields(self):
        envelope = _legacy_envelope_schema()
        ledger = {
            "verified_title": {
                "status": "present",
                "value": "Verified registry title",
                "provenance": "successful web_search receipt",
            },
            "optional_secondary": {
                "status": "degraded",
                "reason": "secondary field was absent",
                "provenance": "successful source response",
            },
        }

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _tool_started(
                "web_search",
                "verified-search",
                query="registry title",
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "verified-search",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "verified-search",
                    "actual_dispatch_attempted": True,
                },
            }
            yield _tool_completed("web_search", "verified-search")
            yield {
                "type": "delta",
                "content": (
                    "# Partial acquisition\n"
                    "COMPLETION_QUALITY_JSON: "
                    '{"status":"degraded","reason":"secondary field absent"}\n'
                    "RESULT_FIELDS_JSON: "
                    + json.dumps(ledger, separators=(",", ":"))
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/partial_acquisition.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "retrieve one primary and one optional field",
                    "step_type": "knowledge_bootstrap",
                    "tools": ["web_search"],
                    "required_capability_tools": ["web_search"],
                    "required_result_fields": [
                        "verified_title",
                        "optional_secondary",
                    ],
                    "required_result_schema": {
                        "verified_title": envelope,
                        "optional_secondary": envelope,
                    },
                },
                _context("web_search"),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("degraded", result["completion_quality"])
        persist_result.assert_called_once()
        audit = result["completion_quality_audit"]
        self.assertTrue(audit["evidence_acquisition_step"])
        self.assertEqual(1, audit["successful_evidence_receipt_count"])
        self.assertFalse(audit["no_verified_value_source"])
        self.assertEqual(
            ["verified_title"],
            audit["populated_typed_value_fields"],
        )
        self.assertEqual([], audit["unverified_typed_value_fields"])

    async def test_worker_with_preloaded_inputs_and_support_skill_needs_no_live_receipt(self):
        skill_body = "# Formatter\nFormat values from declared prior results."
        skill_bytes = skill_body.encode("utf-8")

        async def fake_preload(tool_args, *, context, progress=None):
            return (
                {"success": True, "content": skill_body},
                {
                    "page_count": 1,
                    "total_chars": len(skill_body),
                    "total_bytes": len(skill_bytes),
                    "complete": True,
                    "sha256": hashlib.sha256(skill_bytes).hexdigest(),
                },
            )

        async def fake_registry(tool_name, args, *, context):
            self.assertEqual("read_file", tool_name)
            return json.dumps({
                "success": True,
                "content": "prior verified value",
                "total_lines": 1,
                "offset": 1,
                "limit": 2000,
                "truncated": False,
            })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            prompt = str(messages[0]["content"])
            self.assertNotIn(
                "This is a declared evidence-acquisition step",
                prompt,
            )
            yield {
                "type": "delta",
                "content": (
                    "# Formatted output\n"
                    'COMPLETION_QUALITY_JSON: {"status":"complete"}\n'
                    'RESULT_FIELDS_JSON: {"formatted":"PRIOR VERIFIED VALUE"}'
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch(
                "tools.delegation._load_complete_skill_view_preload",
                fake_preload,
            ),
            patch("tools.delegation.registry_dispatch", fake_registry),
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/formatted_worker.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "format the prior verified value",
                    "step_type": "worker",
                    "tools": ["read_file", "skill_view"],
                    "required_result_paths": ["results/prior.txt"],
                    "required_result_fields": ["formatted"],
                    "required_result_schema": {
                        "formatted": {"type": "string"},
                    },
                    "required_capability_skills": ["formatter-skill"],
                },
                _context("read_file", "skill_view"),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("complete", result["completion_quality"])
        persist_result.assert_called_once()
        audit = result["completion_quality_audit"]
        self.assertFalse(audit["evidence_acquisition_step"])
        self.assertFalse(audit["typed_evidence_dispatch_expected"])
        self.assertFalse(audit["no_verified_value_source"])

    async def test_short_json_result_is_not_rejected_as_free_prose(self):
        body = '{"status":"ok","items":[]}'

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {"type": "delta", "content": body}
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_short.json",
            ),
        ):
            result = await _run_child(
                {"goal": "return one JSON status object"},
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertTrue(result["result_shape"]["structured_value"])
        self.assertFalse(result["result_shape"]["typed_footer"])

    async def test_short_free_prose_facts_transformations_and_labels_are_valid(self):
        cases = (
            (
                "Return one concise physical fact.",
                "水在标准大气压下的沸点是100°C。",
            ),
            (
                'Transform "Hello, world!" to uppercase.',
                "HELLO, WORLD!",
            ),
            (
                "Classify the supplied sentiment with one label.",
                "positive",
            ),
            (
                "Return exactly PASS or FAIL for the supplied check.",
                "PASS",
            ),
            (
                "Classify the supplied state as exactly OK or ERROR.",
                "OK",
            ),
            (
                "Translate `TBD` to Chinese as `待定`.",
                "待定",
            ),
            (
                "Classify using the `success` or `failure` label.",
                "success",
            ),
            (
                "将结果分类为成功或失败。",
                "成功",
            ),
        )

        for index, (goal, body) in enumerate(cases):
            self.assertLess(len(body), 200)

            async def fake_run_stream(model_id, messages, tools, **kwargs):
                yield {"type": "delta", "content": body}
                yield {"type": "done", "finish_reason": "stop"}

            with (
                self.subTest(goal=goal, body=body),
                patch("agent_loop.run_stream", fake_run_stream),
                patch(
                    "tools.delegation.persist_result_for_history",
                    return_value=f"results/delegate_short_prose_{index}.txt",
                ),
            ):
                result = await _run_child(
                    {"goal": goal},
                    _context(),
                    index,
                )

            self.assertEqual("completed", result["status"])
            self.assertTrue(
                result["result_shape"]["semantic_short_result_valid"]
            )
            self.assertTrue(result["result_shape"]["free_prose_value"])
            self.assertIsNone(
                result["result_shape"]["free_prose_audit_reason"]
            )

    async def test_short_free_prose_terminal_non_results_are_rejected(self):
        cases = (
            ("   \n\t", "no substantive output"),
            ("Done.", "status_or_ack_only"),
            ("Success", "status_or_ack_only"),
            ("OK", "status_or_ack_only"),
            ("任务已完成。", "status_or_ack_only"),
            ("TBD", "placeholder_only"),
            ("I will do this later.", "future_action_promise"),
            ("我会稍后处理这个任务。", "future_action_promise"),
            ("... --- ...", "no_semantic_characters"),
            ("blah blah", "meaningless_filler"),
            ("{{RESULT}}", "template_placeholder"),
            (
                '{"content_omitted":{"_chatds_argument_omitted":true}}',
                "compacted_history_placeholder",
            ),
            ("<|assistant|>42", "model_control_protocol"),
        )

        for index, (body, expected_error) in enumerate(cases):
            async def fake_run_stream(model_id, messages, tools, **kwargs):
                yield {"type": "delta", "content": body}
                yield {"type": "done", "finish_reason": "stop"}

            with (
                self.subTest(body=body),
                patch("agent_loop.run_stream", fake_run_stream),
                patch(
                    "tools.delegation.persist_result_for_history",
                    return_value=f"results/should_not_persist_{index}.txt",
                ) as persist_result,
            ):
                result = await _run_child(
                    {"goal": "Return the final delegated result."},
                    _context(),
                    index,
                )

            self.assertEqual("error", result["status"])
            self.assertIn(expected_error, result["error"])
            persist_result.assert_not_called()

    async def test_long_substantive_free_prose_behavior_is_unchanged(self):
        body = (
            "The comparison identifies two independently supported differences, "
            "explains how each observation affects the requested conclusion, and "
            "states the bounded assumptions used in the comparison. " * 3
        )
        self.assertGreater(len(body), 200)

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {"type": "delta", "content": body}
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_long_prose.txt",
            ),
        ):
            result = await _run_child(
                {"goal": "Compare the supplied observations."},
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertTrue(result["result_shape"]["free_prose_value"])
        self.assertFalse(result["result_shape"]["semantic_short_result_valid"])

    async def test_empty_json_containers_are_not_substantive_results(self):
        for body in ("{}", "[]", '{"items":[],"metadata":{}}'):
            async def fake_run_stream(model_id, messages, tools, **kwargs):
                yield {"type": "delta", "content": body}
                yield {"type": "done", "finish_reason": "stop"}

            with self.subTest(body=body), patch(
                "agent_loop.run_stream",
                fake_run_stream,
            ):
                result = await _run_child(
                    {"goal": "return substantive structured evidence"},
                    _context(),
                    0,
                )

            self.assertEqual("error", result["status"])
            self.assertIn("no substantive value", result["error"])
            self.assertFalse(result["result_shape"]["structured_value"])
            self.assertFalse(result["result_shape"]["typed_footer"])

    def test_typed_provenance_and_degraded_reason_require_text(self):
        envelope = _legacy_envelope_schema()
        for record in (
            {
                "status": "present",
                "value": True,
                "provenance": True,
            },
            {
                "status": "degraded",
                "reason": True,
                "provenance": 0,
            },
        ):
            body = "RESULT_FIELDS_JSON: " + json.dumps({"field": record})
            audit = _result_field_audit(
                body,
                ["field"],
                {"field": envelope},
            )
            self.assertFalse(audit["footer_valid"], record)
            self.assertEqual(["field"], audit["missing"])

    async def test_visible_completion_replaces_truncated_prefix_footer(self):
        partial = (
            "# Findings\n"
            "PASS: The retained delegated body records the verified registry "
            "finding, its source provenance, validation status, and the explicit "
            "secondary-source limitation without inventing evidence. " * 4
        ).rstrip() + "\nRESULT_FIELDS_JSON: {"
        ledger = {
            "study_title": {
                "status": "present",
                "value_summary": "Verified registry title",
                "provenance": "bounded registry tool result",
            },
            "enrollment_count": {
                "status": "degraded",
                "reason": "secondary endpoint unavailable",
                "provenance": "registry attempt and fallback audit",
            },
        }
        stale_completion_text = (
            "Conclusion: this is stale continuation text from the rejected "
            "ledger tail.\n"
        )
        replacement_footer = "RESULT_FIELDS_JSON: " + json.dumps(
            ledger,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        retained_partial = partial.rsplit("RESULT_FIELDS_JSON:", 1)[0]
        accumulated = retained_partial + replacement_footer

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            self.assertEqual(
                kwargs.get("required_result_fields"),
                ["study_title", "enrollment_count"],
            )
            yield _tool_started(
                "web_search", "call-visible-length", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-visible-length",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-visible-length",
                },
            }
            yield _tool_completed("web_search", "call-visible-length")
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": partial}
            yield {"type": "delta", "content": "\n"}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "length",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_visible_length_recovery": True,
                "delegate_visible_length_recovery_discard_invalid_tail": True,
            })
            # run_stream buffers the provider completion and, because the
            # previous turn had already entered the ledger, exposes only the
            # unique legal replacement footer to the outer collector.
            yield {"type": "delta", "content": replacement_footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": "delegated_visible_length_recovery",
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_visible_length.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return accumulated typed evidence",
                    "tools": ["web_search"],
                    "required_result_fields": [
                        "study_title",
                        "enrollment_count",
                    ],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["terminal_reason"],
            "delegated_visible_length_recovery",
        )
        self.assertEqual(result["result_path"], "results/delegate_visible_length.md")
        self.assertEqual(
            result["result_field_audit"]["present"],
            ["study_title", "enrollment_count"],
        )
        self.assertEqual(result["result_field_audit"]["degraded"], [])
        self.assertEqual(result["tool_audit"]["successful_tools"], ["web_search"])
        persist_result.assert_called_once()
        self.assertEqual(persist_result.call_args.args[0], accumulated)
        self.assertEqual(
            persist_result.call_args.args[0].count("RESULT_FIELDS_JSON:"),
            1,
        )
        self.assertNotIn(stale_completion_text.strip(), accumulated)

    async def test_footer_bridge_drops_rejected_ledger_tail_before_repair(self):
        prefix_body = (
            "# Evidence\n"
            + "PASS: retained evidence has bounded provenance. " * 8
        )
        prefix = prefix_body + "\nRESULT_FIELDS_JSON: {\n"
        suffix_body = (
            "Conclusion: the unavailable source remains explicitly degraded.\n"
        )
        valid_footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "degraded",
                "reason": "source unavailable",
                "provenance": "attempted source/fallback",
            },
        })

        for suffix in (
            suffix_body,
            suffix_body + "RESULT_FIELDS_JSON: {\n  \"study_title\": {\n",
        ):
            async def fake_run_stream(model_id, messages, tools, **kwargs):
                boundary_sink = kwargs["turn_boundary_sink"]
                await boundary_sink({"phase": "started", "iteration": 1})
                yield {"type": "delta", "content": prefix}
                await boundary_sink({
                    "phase": "finished",
                    "iteration": 1,
                    "finish_reason": "length",
                })
                await boundary_sink({
                    "phase": "started",
                    "iteration": 2,
                    "delegate_visible_length_recovery": True,
                    "delegate_visible_length_recovery_discard_invalid_tail": True,
                })
                # Once the prefix has entered RESULT_FIELDS_JSON, run_stream
                # treats a completion lacking a unique legal footer as the
                # rejected ledger tail and withholds it atomically.
                yield {"type": "delta", "content": "\n"}
                await boundary_sink({
                    "phase": "finished",
                    "iteration": 2,
                    "finish_reason": "stop",
                })
                await boundary_sink({
                    "phase": "started",
                    "iteration": 3,
                    "delegate_result_footer_repair": True,
                    "delegate_result_footer_repair_origin": (
                        "visible_length_recovery"
                    ),
                    "delegate_result_footer_repair_discard_invalid_tail": True,
                })
                yield {"type": "delta", "content": valid_footer}
                await boundary_sink({
                    "phase": "finished",
                    "iteration": 3,
                    "finish_reason": "stop",
                })
                yield {
                    "type": "agent_event",
                    "event_type": "run.completed",
                    "payload": {
                        "finish_reason": "stop",
                        "terminal_reason": "delegated_visible_length_recovery",
                    },
                }
                yield {"type": "done", "finish_reason": "stop"}

            with self.subTest(multiline_candidate="study_title" in suffix):
                with (
                    patch("agent_loop.run_stream", fake_run_stream),
                    patch(
                        "tools.delegation.persist_result_for_history",
                        return_value="results/delegate_footer_bridge.md",
                    ) as persist_result,
                ):
                    result = await _run_child(
                        {
                            "goal": "return retained typed evidence",
                            "required_result_fields": ["study_title"],
                        },
                        _context(),
                        0,
                    )

            self.assertEqual("completed", result["status"])
            persisted = persist_result.call_args.args[0]
            self.assertEqual(1, persisted.count("RESULT_FIELDS_JSON:"))
            self.assertIn(prefix_body, persisted)
            self.assertNotIn(suffix_body, persisted)
            self.assertTrue(persisted.rstrip().endswith(valid_footer))

    async def test_visible_continuation_replaces_even_valid_prefix_footer(self):
        prefix_body = (
            "# Evidence\n" + "Verified bounded provenance is retained. " * 8
        )
        old_footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Old terminal sample",
                "provenance": "registry",
            },
        })
        stale_completion_text = "Conclusion: final bounded status."
        replacement_footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Final terminal sample",
                "provenance": "registry",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {
                "type": "delta",
                "content": prefix_body + "\n" + old_footer + "\n",
            }
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "length",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_visible_length_recovery": True,
                "delegate_visible_length_recovery_discard_invalid_tail": True,
            })
            yield {"type": "delta", "content": replacement_footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": "delegated_visible_length_recovery",
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_replaced_prefix_footer.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return one final typed ledger",
                    "required_result_fields": ["study_title"],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        persisted = persist_result.call_args.args[0]
        self.assertEqual(1, persisted.count("RESULT_FIELDS_JSON:"))
        self.assertNotIn("Old terminal sample", persisted)
        self.assertNotIn(stale_completion_text, persisted)
        self.assertIn("Final terminal sample", persisted)

    async def test_ordinary_footer_repair_replaces_malformed_terminal_candidate(self):
        body = (
            "# Evidence\n"
            + "PASS: retained bounded evidence with provenance. " * 8
        )
        malformed = 'RESULT_FIELDS_JSON: {"obsolete_bad_key":'
        valid_footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Verified title",
                "provenance": "bounded registry result",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": body + "\n" + malformed}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "stop",
            })
            yield {"type": "delta", "content": "\n"}
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_result_footer_repair": True,
                "delegate_result_footer_repair_origin": "ordinary_stop",
                "delegate_result_footer_repair_discard_invalid_tail": True,
            })
            yield {"type": "delta", "content": valid_footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": "delegated_result_footer_repair",
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_ordinary_footer_repair.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return one typed result",
                    "required_result_fields": ["study_title"],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        persisted = persist_result.call_args.args[0]
        self.assertIn(body, persisted)
        self.assertNotIn(malformed, persisted)
        self.assertEqual(1, persisted.count("RESULT_FIELDS_JSON:"))
        self.assertTrue(persisted.rstrip().endswith(valid_footer))

    async def test_structured_projection_replaces_protocol_contaminated_turn(self):
        contaminated = (
            "# Inventory reconciliation\n"
            "The bounded warehouse receipts were reviewed.\n"
            "<tool_call><arguments>{\"query\":\"pending\"}"
            "</arguments></tool_call>"
        )
        valid_footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "shipment_status": {
                "status": "degraded",
                "reason": "one registry remained unavailable",
                "provenance": "bounded receipt ledger",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": contaminated}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "stop",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_result_footer_repair": True,
                "delegate_result_footer_repair_origin": (
                    "raw_pseudo_tool_protocol_evidence_projection"
                ),
                "delegate_result_footer_repair_discard_invalid_tail": True,
                (
                    "delegate_result_footer_repair_"
                    "replace_invalid_source_turn"
                ): True,
            })
            # The provider's internal submitter call is control-plane only.
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "tool_calls",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_result_footer_repair": True,
                "delegate_result_footer_repair_origin": (
                    "raw_pseudo_tool_protocol_evidence_projection"
                ),
                "delegate_result_footer_repair_discard_invalid_tail": True,
                "internal_structured_submitter_release": True,
            })
            yield {"type": "delta", "content": "\n" + valid_footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": (
                        "delegated_result_footer_structured_repair"
                    ),
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_inventory_projection.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return typed shipment evidence",
                    "required_result_fields": ["shipment_status"],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(
            "delegated_result_footer_structured_repair",
            result["terminal_reason"],
        )
        persisted = persist_result.call_args.args[0]
        self.assertNotIn("<tool_call>", persisted)
        self.assertNotIn("Inventory reconciliation", persisted)
        self.assertEqual(valid_footer, persisted.strip())
        self.assertEqual(0, result["output_protocol_audit"]["detected_count"])

    async def test_visible_footer_bridge_failure_is_retryable_without_mutation(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {
                "type": "delta",
                "content": "# Evidence\n" + "Bounded provenance retained. " * 10,
            }
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "length",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_visible_length_recovery": True,
            })
            yield {
                "type": "delta",
                "content": "\nConclusion retained but typed footer missing.\n",
            }
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 3,
                "delegate_result_footer_repair": True,
                "delegate_result_footer_repair_origin": (
                    "visible_length_recovery"
                ),
                "delegate_result_footer_repair_discard_invalid_tail": True,
            })
            await boundary_sink({
                "phase": "finished",
                "iteration": 3,
                "finish_reason": "stop",
            })
            error = (
                "The single footer-only repair did not emit one valid terminal "
                "RESULT_FIELDS_JSON line."
            )
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": error,
                    "finish_reason": (
                        "delegated_visible_length_footer_repair_invalid"
                    ),
                    "failure_class": "agent_contract_noncompliance",
                    "retryable": False,
                },
            }
            yield {"type": "error", "msg": error}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return typed evidence",
                    "required_result_fields": ["study_title"],
                },
                _context(),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertTrue(result["retryable"])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_invalid_accumulated_recovery_footer_is_retryable_without_mutation(self):
        partial = (
            "# Findings\n"
            "PASS: The retained delegated body records verified evidence, source "
            "provenance, validation status, explicit limitations, and degraded "
            "gaps for downstream review without inventing any unavailable fact. " * 4
        ).rstrip()
        invalid_completion = (
            "Conclusion: the bounded review is complete.\n"
            "RESULT_FIELDS_JSON: "
            + json.dumps({
                "wrong_key": {
                    "status": "present",
                    "value_summary": "not contract-valid",
                    "provenance": "bounded result",
                },
            }, separators=(",", ":"))
        )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _tool_started(
                "web_search", "call-invalid-footer", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-invalid-footer",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-invalid-footer",
                },
            }
            yield _tool_completed("web_search", "call-invalid-footer")
            yield {"type": "delta", "content": partial}
            yield {"type": "delta", "content": "\n"}
            yield {"type": "delta", "content": invalid_completion}
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": "delegated_visible_length_recovery",
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return accumulated typed evidence",
                    "tools": ["web_search"],
                    "required_result_fields": ["study_title"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["result_path"])
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["result_excerpt"], "")
        persist_result.assert_not_called()
        self.assertTrue(result["retryable"])
        self.assertEqual(
            result["failure_class"],
            "agent_contract_noncompliance",
        )
        self.assertEqual(
            result["terminal_reason"],
            "delegated_visible_length_recovery",
        )
        self.assertFalse(result["result_field_audit"]["footer_valid"])
        self.assertEqual(result["result_field_audit"]["missing"], ["study_title"])

    async def test_unnamed_missing_field_is_not_waived_by_generic_gap(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            ledger = {
                "study_title": {
                    "status": "present",
                    "value_summary": "Verified example",
                    "provenance": "registry record",
                },
                "enrollment_count": {
                    "status": "degraded",
                },
            }
            yield {
                "type": "delta",
                "content": (
                    "# Findings\n"
                    "- study_title: Verified example.\n"
                    "# Gaps\n"
                    "Some upstream data was unavailable, so the report is degraded. " * 6
                    + "\nRESULT_FIELDS_JSON: "
                    + json.dumps(ledger, ensure_ascii=False, separators=(",", ":"))
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_missing.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "extract typed evidence",
                    "required_result_fields": [
                        "study_title",
                        "enrollment_count",
                    ],
                    "required_result_schema": {
                        "study_title": _legacy_envelope_schema(),
                        "enrollment_count": _legacy_envelope_schema(),
                    },
                },
                _context(),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["result_path"])
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["result_excerpt"], "")
        self.assertEqual(result["reasoning_summary"], "")
        persist_result.assert_not_called()
        self.assertIn("enrollment_count", result["error"])
        self.assertEqual(
            result["result_field_audit"]["missing"],
            ["enrollment_count"],
        )

    async def test_process_narration_only_is_rejected_even_when_long(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "Let me search the first database. I will query another endpoint. "
                    "Next I need to inspect the returned pages. Now I will check more records. "
                    "Excellent finding: PMID 123456 appears relevant. I should continue "
                    "searching before I prepare the answer and complete the evidence set. " * 6
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_narration.md",
            ),
        ):
            result = await _run_child(
                {"goal": "return final evidence"},
                _context(),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("process narration", result["error"])
        self.assertEqual(result["terminal_reason"], "delegated_output_contract_failed")

    async def test_unaudited_raw_pseudo_tool_call_is_retryable_noncompliance(self):
        observed: dict[str, str] = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed["prompt"] = str(messages[0]["content"])
            yield {
                "type": "delta",
                "content": (
                    "# Evidence\n"
                    "The bounded evidence review is complete with explicit provenance, "
                    "verification, and a degraded gap for the unavailable source. " * 4
                    + "\n<tool_call>execute_code>\n"
                    + '{"code":"print(\'not actually executed\')"}\n'
                    + "</execute_code>"
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_pseudo_tool.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return final bounded evidence",
                    "tools": ["web_search"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["result_path"])
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["result_excerpt"], "")
        self.assertEqual(result["reasoning_summary"], "")
        persist_result.assert_not_called()
        self.assertEqual(result["failure_class"], "agent_contract_noncompliance")
        self.assertTrue(result["retryable"])
        self.assertEqual(
            result["terminal_reason"],
            "delegated_output_contract_failed",
        )
        self.assertIn("raw pseudo-tool protocol markup", result["error"])
        self.assertEqual(
            result["output_protocol_audit"]["unsupported_tool_names"],
            ["execute_code"],
        )
        self.assertEqual(result["tool_audit"]["successful_tools"], [])
        self.assertIn("never print raw XML/tool-call protocol markup", observed["prompt"])

    async def test_interim_tool_turn_protocol_is_not_part_of_terminal_result(self):
        final_body = (
            "# Evidence\n"
            + (
                "Verified terminal evidence with provenance and an explicit "
                "degraded gap for unavailable data. " * 6
            )
            + "\nRESULT_FIELDS_JSON: "
            + json.dumps({
                "target_id": {
                    "status": "present",
                    "value_summary": "ENSG000001",
                    "provenance": "verified registry result",
                },
            })
        )
        interim = (
            "I will query another source.\n"
            "<tool_call><arguments>{\"query\":\"pending\"}</arguments></tool_call>"
        )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "agent_event",
                "event_type": "debug.iteration.started",
                "payload": {"iteration": 1},
            }
            yield {"type": "delta", "content": interim}
            yield {
                "type": "agent_event",
                "event_type": "debug.llm.finish",
                "payload": {"iteration": 1, "finish_reason": "tool_calls"},
            }
            yield {
                "type": "agent_event",
                "event_type": "debug.iteration.started",
                "payload": {"iteration": 2},
            }
            yield {"type": "delta", "content": final_body}
            yield {
                "type": "agent_event",
                "event_type": "debug.llm.finish",
                "payload": {"iteration": 2, "finish_reason": "stop"},
            }
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {"finish_reason": "stop"},
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_terminal_only.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return final typed evidence",
                    "required_result_fields": ["target_id"],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(len(final_body), result["result_chars"])
        self.assertEqual(0, result["output_protocol_audit"]["detected_count"])
        persisted_content = persist_result.call_args.args[0]
        self.assertEqual(final_body, persisted_content)
        self.assertNotIn("<tool_call>", persisted_content)

    async def test_turn_isolation_does_not_depend_on_debug_events(self):
        final_body = (
            "# Evidence\n"
            + "Verified terminal evidence with provenance. " * 8
            + "\nRESULT_FIELDS_JSON: "
            + json.dumps({
                "target_id": {
                    "status": "present",
                    "value_summary": "ENSG000001",
                    "provenance": "verified registry result",
                },
            })
        )
        interim = (
            "Calling the source now.\n"
            "<tool_call><arguments>{\"query\":\"pending\"}</arguments></tool_call>"
        )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": interim}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "tool_calls",
            })
            await boundary_sink({"phase": "started", "iteration": 2})
            yield {"type": "delta", "content": final_body}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {"finish_reason": "stop"},
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_debug_independent.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return final typed evidence",
                    "required_result_fields": ["target_id"],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(final_body, persist_result.call_args.args[0])
        self.assertNotIn("<tool_call>", persist_result.call_args.args[0])

    async def test_nonterminal_stop_continuation_is_not_in_final_result(self):
        rejected_body = "I will continue because this draft is incomplete."
        final_body = (
            "# Final evidence\n"
            + "Verified terminal evidence with provenance. " * 8
            + "\nRESULT_FIELDS_JSON: "
            + json.dumps({
                "target_id": {
                    "status": "present",
                    "value_summary": "ENSG000001",
                    "provenance": "verified registry result",
                },
            })
        )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": rejected_body}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "stop",
            })
            await boundary_sink({"phase": "started", "iteration": 2})
            yield {"type": "delta", "content": final_body}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_terminal_stop.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return final typed evidence",
                    "required_result_fields": ["target_id"],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(final_body, persist_result.call_args.args[0])
        self.assertNotIn(rejected_body, persist_result.call_args.args[0])

    async def test_terminal_stop_body_and_footer_repair_are_combined(self):
        body = "# Evidence\n" + "Verified evidence with provenance. " * 10
        footer = "\nRESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Bounded Study",
                "provenance": "registry",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": body}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "stop",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_result_footer_repair": True,
            })
            yield {"type": "delta", "content": footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_stop_footer.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return final typed evidence",
                    "required_result_fields": ["study_title"],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(body + footer, persist_result.call_args.args[0])

    async def test_terminal_length_body_and_footer_repair_turn_are_combined(self):
        partial_body = (
            "# Evidence\n"
            + "Verified evidence and provenance are retained. " * 8
        )
        footer = "\nRESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Bounded Study",
                "provenance": "registry",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "agent_event",
                "event_type": "debug.iteration.started",
                "payload": {"iteration": 1},
            }
            yield {"type": "delta", "content": partial_body}
            yield {
                "type": "agent_event",
                "event_type": "debug.llm.finish",
                "payload": {"iteration": 1, "finish_reason": "length"},
            }
            yield {
                "type": "agent_event",
                "event_type": "debug.iteration.started",
                "payload": {"iteration": 2},
            }
            yield {"type": "delta", "content": footer}
            yield {
                "type": "agent_event",
                "event_type": "debug.llm.finish",
                "payload": {"iteration": 2, "finish_reason": "stop"},
            }
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": "delegated_visible_length_recovery",
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_length_recovered.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return final typed evidence",
                    "required_result_fields": ["study_title"],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(partial_body + footer, persist_result.call_args.args[0])
        self.assertTrue(result["result_field_audit"]["footer_valid"])

    async def test_post_dispatch_synthesis_length_continuation_is_combined(self):
        clean_partial_body = (
            "# Evidence\n"
            + "The retained registry evidence includes explicit provenance. " * 8
        )
        stale_footer = "RESULT_FIELDS_JSON: {\"study_title\": "
        partial_body = clean_partial_body + "\n" + stale_footer
        completion = "\nConclusion: no unsupported fact was added.\n"
        footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Bounded Study",
                "provenance": "registry result",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            yield _tool_started(
                "web_search", "call-post-length", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-post-length",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-post-length",
                },
            }
            yield _tool_completed("web_search", "call-post-length")
            await boundary_sink({
                "phase": "started",
                "iteration": 1,
                "delegate_post_dispatch_stream_synthesis": True,
            })
            yield {"type": "delta", "content": partial_body}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "length",
                "abandon_reason": (
                    "post_dispatch_synthesis_stream_interrupted_after_visible_prefix"
                ),
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_synthesis_length_continuation": True,
                "delegate_post_dispatch_synthesis_length_continuation": True,
                "delegate_synthesis_length_continuation_discard_invalid_tail": True,
            })
            yield {"type": "delta", "content": completion + footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": (
                        "post_dispatch_stream_recovery_synthesis"
                    ),
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_post_length.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return recovered typed evidence",
                    "tools": ["web_search"],
                    "required_result_fields": ["study_title"],
                },
                _context("web_search"),
                0,
            )

        expected = clean_partial_body + "\n" + completion + footer
        self.assertEqual("completed", result["status"])
        self.assertEqual(expected, persist_result.call_args.args[0])
        self.assertEqual(
            1,
            persist_result.call_args.args[0].count("# Evidence"),
        )
        self.assertEqual(
            1,
            persist_result.call_args.args[0].count(
                "Conclusion: no unsupported fact was added."
            ),
        )
        self.assertEqual(
            1,
            persist_result.call_args.args[0].count("RESULT_FIELDS_JSON:"),
        )
        self.assertNotIn(
            stale_footer,
            persist_result.call_args.args[0].splitlines(),
        )
        self.assertTrue(result["result_field_audit"]["footer_valid"])
        self.assertEqual(
            ["web_search"],
            result["tool_audit"]["successful_tools"],
        )
        self.assertEqual(
            "post_dispatch_stream_recovery_synthesis",
            result["terminal_reason"],
        )
        self.assertEqual("", result["reasoning_summary"])

    async def test_post_dispatch_interrupted_generation_is_discarded_before_persist(self):
        observed_runtime_contract = {}
        abandoned_content = "ABANDONED-MIXED-PARTIAL-MUST-NOT-PERSIST"
        abandoned_reasoning = "ABANDONED-MIXED-REASONING-MUST-NOT-PERSIST"
        final_body = (
            "# Evidence\n"
            + (
                "The committed registry result has explicit provenance, while "
                "unavailable evidence is recorded as a bounded degraded gap. "
                * 8
            )
        )
        footer = "\nRESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Bounded Study",
                "provenance": "committed registry result",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed_runtime_contract["required_capability_tools"] = kwargs.get(
                "required_capability_tools"
            )
            boundary_sink = kwargs["turn_boundary_sink"]
            yield _tool_started(
                "web_search", "call-before-interrupt", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-before-interrupt",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-before-interrupt",
                    "actual_dispatch_attempted": True,
                },
            }
            yield _tool_completed("web_search", "call-before-interrupt")
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": abandoned_content}
            yield {
                "type": "reasoning_delta",
                "content": abandoned_reasoning,
            }
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "abandoned",
                "abandon_reason": (
                    "post_dispatch_generation_stream_interrupted"
                ),
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_post_dispatch_stream_synthesis": True,
            })
            yield {"type": "delta", "content": final_body + footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": (
                        "post_dispatch_stream_recovery_synthesis"
                    ),
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_interrupted_rebuilt.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return recovered typed evidence",
                    "tools": ["web_search"],
                    "required_capability_tools": ["web_search"],
                    "required_result_fields": ["study_title"],
                },
                _context("web_search"),
                0,
            )

        persisted = persist_result.call_args.args[0]
        self.assertEqual("completed", result["status"])
        self.assertEqual(final_body + footer, persisted)
        self.assertNotIn(abandoned_content, persisted)
        self.assertNotIn(abandoned_reasoning, result["reasoning_summary"])
        self.assertEqual(
            ["web_search"], result["tool_audit"]["attempted_tools"]
        )
        self.assertEqual(
            ["web_search"], result["tool_audit"]["successful_tools"]
        )
        self.assertEqual(
            ["web_search"],
            observed_runtime_contract["required_capability_tools"],
        )
        self.assertEqual("complete", result["completion_quality"])
        self.assertEqual(
            1,
            result["completion_quality_audit"][
                "attempted_evidence_receipt_count"
            ],
        )
        self.assertEqual(
            1,
            result["completion_quality_audit"][
                "successful_evidence_receipt_count"
            ],
        )
        persist_result.assert_called_once()

    async def test_clean_visible_recovery_turn_isolation_discards_abandoned_suffix(self):
        retained_prefix = (
            "# Retained evidence\n"
            + "Bounded evidence has explicit provenance. " * 10
        )
        abandoned_suffix = "ABANDONED-INTERRUPTED-SUFFIX-MUST-NOT-PERSIST"
        abandoned_reasoning = "ABANDONED-HIDDEN-REASONING-MUST-NOT-PERSIST"
        completion = "\nConclusion: unavailable evidence remains degraded.\n"
        footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "degraded",
                "reason": "unavailable in bounded evidence",
                "provenance": "attempted source/fallback",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "reasoning_delta", "content": abandoned_reasoning}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "abandoned",
                "abandon_reason": "stream_interrupted_after_reasoning",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_reasoning_only_recovery": True,
            })
            yield {"type": "delta", "content": retained_prefix}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "length",
                "abandon_reason": (
                    "reasoning_recovery_stream_interrupted_after_visible_prefix"
                ),
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 3,
                "delegate_visible_length_recovery": True,
                "delegate_visible_recovery_origin": (
                    "reasoning_only_stream_recovery"
                ),
            })
            # This suffix represents bytes buffered by an interrupted
            # continuation.  A later terminal sample is included only to make
            # persistence observable and prove the abandoned suffix is absent.
            yield {"type": "delta", "content": abandoned_suffix}
            await boundary_sink({
                "phase": "finished",
                "iteration": 3,
                "finish_reason": "abandoned",
                "abandon_reason": (
                    "clean_visible_stream_continuation_interrupted"
                ),
            })
            await boundary_sink({"phase": "started", "iteration": 4})
            yield {"type": "delta", "content": completion + footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 4,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": "delegated_visible_length_recovery",
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_clean_visible.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return bounded typed evidence",
                    "required_result_fields": ["study_title"],
                },
                _context(),
                0,
            )

        expected = retained_prefix + completion + footer
        self.assertEqual("completed", result["status"])
        self.assertEqual(expected, persist_result.call_args.args[0])
        self.assertEqual(1, persist_result.call_args.args[0].count(
            "# Retained evidence"
        ))
        self.assertNotIn(abandoned_suffix, persist_result.call_args.args[0])
        self.assertNotIn(abandoned_reasoning, result["reasoning_summary"])
        self.assertTrue(result["result_field_audit"]["footer_valid"])

    async def test_clean_visible_recovery_failure_is_never_persisted(self):
        retained_prefix = (
            "# Retained recovery prefix\n" + "bounded evidence " * 30
        )
        abandoned_suffix = "INTERRUPTED-CONTINUATION-MUST-NOT-PERSIST"

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "reasoning_delta", "content": "discarded reasoning"}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "abandoned",
                "abandon_reason": "stream_interrupted_after_reasoning",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_reasoning_only_recovery": True,
            })
            yield {"type": "delta", "content": retained_prefix}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "length",
                "abandon_reason": (
                    "reasoning_recovery_stream_interrupted_after_visible_prefix"
                ),
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 3,
                "delegate_visible_length_recovery": True,
                "delegate_visible_recovery_origin": (
                    "reasoning_only_stream_recovery"
                ),
            })
            yield {"type": "delta", "content": abandoned_suffix}
            await boundary_sink({
                "phase": "finished",
                "iteration": 3,
                "finish_reason": "abandoned",
                "abandon_reason": (
                    "clean_visible_stream_continuation_interrupted"
                ),
            })
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "bounded continuation interrupted",
                    "finish_reason": "stream_interrupted_after_partial",
                    "terminal_reason": "stream_interrupted_after_partial",
                },
            }
            yield {
                "type": "error",
                "msg": "bounded continuation interrupted",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {"goal": "return bounded evidence"},
                _context(),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "stream_interrupted_after_partial",
            result["terminal_reason"],
        )
        self.assertIsNone(result["result_path"])
        self.assertNotIn(abandoned_suffix, result["result_excerpt"])
        persist_result.assert_not_called()

    async def test_post_dispatch_exact_prefix_replay_failure_is_not_persisted(self):
        retained_prefix = (
            "# Evidence\n"
            + "The first interrupted synthesis prefix has provenance. " * 8
        )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            yield _tool_started(
                "web_search", "call-post-interrupt-fail", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-post-interrupt-fail",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-post-interrupt-fail",
                },
            }
            yield _tool_completed("web_search", "call-post-interrupt-fail")
            await boundary_sink({
                "phase": "started",
                "iteration": 1,
                "delegate_post_dispatch_stream_synthesis": True,
            })
            yield {"type": "delta", "content": retained_prefix + "\n"}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "length",
                "abandon_reason": (
                    "post_dispatch_synthesis_stream_interrupted_after_visible_prefix"
                ),
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_synthesis_length_continuation": True,
                "delegate_post_dispatch_synthesis_length_continuation": True,
            })
            # The provider returned the exact retained prefix and stopped. The
            # agent loop's overlap filter emitted no duplicate delta, then its
            # non-empty-unique-suffix gate failed this terminal continuation.
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "The bounded continuation added no unique suffix.",
                    "finish_reason": (
                        "provider_tool_stream_post_dispatch_synthesis_failed"
                    ),
                    "terminal_reason": (
                        "provider_tool_stream_post_dispatch_synthesis_failed"
                    ),
                },
            }
            yield {
                "type": "error",
                "msg": "The bounded continuation added no unique suffix.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return recovered typed evidence",
                    "tools": ["web_search"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "provider_tool_stream_post_dispatch_synthesis_failed",
            result["terminal_reason"],
        )
        self.assertEqual("", result["summary"])
        self.assertEqual("", result["result_excerpt"])
        self.assertIsNone(result["result_path"])
        self.assertFalse(result["retryable"])
        self.assertEqual("provider_protocol", result["failure_class"])
        persist_result.assert_not_called()

    async def test_abandoned_reasoning_is_excluded_from_outer_child_result(self):
        secret = "abandoned-provider-reasoning-must-not-persist"
        final_body = (
            "# Evidence\n"
            + (
                "The retained registry result has explicit provenance, while "
                "unavailable evidence is recorded as a bounded degraded gap. "
                * 8
            )
        )
        footer = "\nRESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Bounded Study",
                "provenance": "retained registry result",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            yield _tool_started(
                "web_search", "call-abandoned-reasoning", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-abandoned-reasoning",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-abandoned-reasoning",
                },
            }
            yield _tool_completed("web_search", "call-abandoned-reasoning")
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "reasoning_delta", "content": secret}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "abandoned",
                "abandon_reason": "stream_interrupted_after_reasoning",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_post_dispatch_stream_synthesis": True,
            })
            yield {"type": "delta", "content": final_body + footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": (
                        "post_dispatch_stream_recovery_synthesis"
                    ),
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_abandoned_reasoning.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return recovered typed evidence",
                    "tools": ["web_search"],
                    "required_result_fields": ["study_title"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("", result["reasoning_summary"])
        self.assertNotIn(secret, result["summary"])
        self.assertEqual(final_body + footer, persist_result.call_args.args[0])
        self.assertTrue(result["result_field_audit"]["footer_valid"])
        self.assertEqual(
            ["web_search"], result["tool_audit"]["successful_tools"]
        )

    async def test_post_dispatch_contract_repair_discards_invalid_draft(self):
        invalid_draft = (
            "draft missing typed fields\n"
            "<tool_call><arguments>{\"query\":\"retry\"}"
            "</arguments></tool_call>"
        )
        clean_body = (
            "# Repaired evidence\n"
            + (
                "The retained tool result has explicit provenance, while every "
                "unavailable fact is recorded as a bounded degraded gap. " * 6
            )
        )
        footer = "\nRESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "degraded",
                "reason": "not present in retained evidence",
                "provenance": "attempted source/fallback",
            },
        })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            yield _tool_started(
                "web_search", "call-contract-repair", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-contract-repair",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-contract-repair",
                },
            }
            yield _tool_completed("web_search", "call-contract-repair")
            await boundary_sink({
                "phase": "started",
                "iteration": 1,
                "delegate_post_dispatch_stream_synthesis": True,
                "delegate_post_dispatch_synthesis_terminal_contract_audit": True,
            })
            yield {"type": "delta", "content": invalid_draft}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "stop",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_output_contract_repair": True,
            })
            yield {"type": "delta", "content": clean_body + footer}
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": "delegated_output_contract_repair",
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_contract_repaired.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return repaired typed evidence",
                    "tools": ["web_search"],
                    "required_result_fields": ["study_title"],
                },
                _context("web_search"),
                0,
            )

        repaired_result = clean_body + footer
        self.assertEqual("completed", result["status"])
        self.assertEqual(repaired_result, persist_result.call_args.args[0])
        self.assertNotIn(invalid_draft, result["summary"])
        self.assertEqual(0, result["output_protocol_audit"]["detected_count"])
        self.assertTrue(result["result_field_audit"]["footer_valid"])
        self.assertEqual(
            ["web_search"], result["tool_audit"]["successful_tools"]
        )
        self.assertEqual(
            "delegated_output_contract_repair",
            result["terminal_reason"],
        )

    async def test_output_contract_repair_length_continuation_persists_once(self):
        invalid_draft = (
            "rejected draft\n"
            "<tool_call><name>execute_code</name><arguments>"
            '{"code":"print(1)"}</arguments></tool_call>'
        )
        repair_prefix = (
            "# Rebuilt evidence\n"
            "The retained registry evidence has bounded provenance and every "
            "unavailable value is explicitly degraded."
        )
        repair_suffix = (
            "Conclusion: the bounded evidence result is complete.\n"
            "RESULT_FIELDS_JSON: "
            + json.dumps({
                "study_title": {
                    "status": "degraded",
                    "reason": "not present in retained evidence",
                    "provenance": "attempted source/fallback",
                },
            })
        )
        accumulated = repair_prefix + "\n" + repair_suffix

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            yield _tool_started(
                "web_search", "call-repair-length", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-repair-length",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-repair-length",
                },
            }
            yield _tool_completed("web_search", "call-repair-length")
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": invalid_draft}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "stop",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_output_contract_repair": True,
            })
            # The repair prefix is buffered inside run_stream and therefore
            # never crosses the outer delegated-result boundary on length.
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "length",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 3,
                "delegate_visible_length_recovery": True,
                "delegate_visible_recovery_origin": "output_contract_repair",
            })
            # A successful transactional completion releases the accumulated
            # repair exactly once, rather than separate prefix/suffix deltas.
            yield {"type": "delta", "content": accumulated}
            await boundary_sink({
                "phase": "finished",
                "iteration": 3,
                "finish_reason": "stop",
            })
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": (
                        "delegated_output_contract_repair_length_continuation"
                    ),
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_repair_length.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return one repaired typed evidence result",
                    "tools": ["web_search"],
                    "required_result_fields": ["study_title"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(
            "delegated_output_contract_repair_length_continuation",
            result["terminal_reason"],
        )
        self.assertEqual(len(accumulated), result["result_chars"])
        self.assertEqual(
            "results/delegate_repair_length.md", result["result_path"]
        )
        self.assertTrue(result["result_field_audit"]["footer_valid"])
        self.assertEqual(
            ["web_search"], result["tool_audit"]["successful_tools"]
        )
        persist_result.assert_called_once_with(
            accumulated,
            "delegate_worker",
            user_id="typed-user",
            session_id="typed-session",
        )
        self.assertNotIn(invalid_draft, persist_result.call_args.args[0])
        self.assertEqual(
            1,
            persist_result.call_args.args[0].count("RESULT_FIELDS_JSON:"),
        )

    async def test_output_contract_repair_second_length_is_atomic(self):
        invalid_draft = (
            "rejected draft\n"
            "<tool_call><arguments>{}</arguments></tool_call>"
        )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            yield _tool_started(
                "web_search", "call-repair-second-length", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-repair-second-length",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-repair-second-length",
                },
            }
            yield _tool_completed("web_search", "call-repair-second-length")
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": invalid_draft}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "stop",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_output_contract_repair": True,
            })
            # Both provider samples remain inside run_stream's transactional
            # buffer, so neither partial body is exposed as an outer delta.
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "length",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 3,
                "delegate_visible_length_recovery": True,
                "delegate_visible_recovery_origin": "output_contract_repair",
            })
            await boundary_sink({
                "phase": "finished",
                "iteration": 3,
                "finish_reason": "length",
            })
            error = (
                "Provider did not complete the single buffered delegated "
                "output-contract repair continuation. Both repair samples "
                "were discarded atomically."
            )
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": error,
                    "finish_reason": (
                        "delegated_output_contract_repair_continuation_failed"
                    ),
                },
            }
            yield {"type": "error", "msg": error}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return one repaired typed evidence result",
                    "tools": ["web_search"],
                    "required_result_fields": ["study_title"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "delegated_output_contract_repair_continuation_failed",
            result["terminal_reason"],
        )
        self.assertEqual(0, result["result_chars"])
        self.assertEqual("", result["summary"])
        self.assertEqual("", result["result_excerpt"])
        self.assertIsNone(result["result_path"])
        self.assertTrue(result["retryable"])
        self.assertEqual(
            "agent_contract_noncompliance",
            result["failure_class"],
        )
        persist_result.assert_not_called()

    async def test_output_contract_repair_raw_continuation_is_atomic_outer(self):
        invalid_draft = (
            "rejected draft\n"
            "<tool_call><arguments>{}</arguments></tool_call>"
        )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            boundary_sink = kwargs["turn_boundary_sink"]
            yield _tool_started(
                "web_search", "call-repair-raw-tail", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-repair-raw-tail",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-repair-raw-tail",
                },
            }
            yield _tool_completed("web_search", "call-repair-raw-tail")
            await boundary_sink({"phase": "started", "iteration": 1})
            yield {"type": "delta", "content": invalid_draft}
            await boundary_sink({
                "phase": "finished",
                "iteration": 1,
                "finish_reason": "stop",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 2,
                "delegate_output_contract_repair": True,
            })
            await boundary_sink({
                "phase": "finished",
                "iteration": 2,
                "finish_reason": "length",
            })
            await boundary_sink({
                "phase": "started",
                "iteration": 3,
                "delegate_visible_length_recovery": True,
                "delegate_visible_recovery_origin": "output_contract_repair",
            })
            # The contaminated suffix is rejected by run_stream before an
            # outer delta can be emitted.
            await boundary_sink({
                "phase": "finished",
                "iteration": 3,
                "finish_reason": "stop",
            })
            error = (
                "The single buffered delegated output-contract repair "
                "continuation failed its terminal protocol contract. Both "
                "repair samples were discarded atomically."
            )
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": error,
                    "finish_reason": (
                        "delegated_output_contract_repair_continuation_invalid"
                    ),
                    "failure_class": "agent_contract_noncompliance",
                    "retryable": False,
                },
            }
            yield {"type": "error", "msg": error}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return one repaired typed evidence result",
                    "tools": ["web_search"],
                    "required_result_fields": ["study_title"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "delegated_output_contract_repair_continuation_invalid",
            result["terminal_reason"],
        )
        self.assertEqual(0, result["result_chars"])
        self.assertEqual("", result["summary"])
        self.assertEqual("", result["result_excerpt"])
        self.assertIsNone(result["result_path"])
        self.assertTrue(result["retryable"])
        persist_result.assert_not_called()

    async def test_recovery_missing_footer_without_mutation_is_retryable(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Evidence\n"
                    "The earlier registry result is preserved with explicit "
                    "provenance, while the unavailable source is recorded as a "
                    "WARN/degraded gap without inventing evidence. " * 6
                ),
            }
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": (
                        "post_dispatch_stream_recovery_synthesis"
                    ),
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return typed bounded evidence",
                    "required_result_fields": ["study_title"],
                },
                _context(),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()
        self.assertTrue(result["retryable"])
        self.assertEqual(
            "agent_contract_noncompliance",
            result["failure_class"],
        )
        self.assertEqual(
            "post_dispatch_stream_recovery_synthesis",
            result["terminal_reason"],
        )
        self.assertIn("RESULT_FIELDS_JSON", result["error"])

    async def test_read_only_output_repair_failure_is_retryable(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _tool_started(
                "web_search", "call-output-repair", query="registry"
            )
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "call-output-repair",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "call-output-repair",
                },
            }
            yield _tool_completed("web_search", "call-output-repair")
            yield {
                "type": "delta",
                "content": (
                    "# Evidence\n"
                    "The bounded replacement preserves registry provenance and "
                    "explicitly records unavailable facts as degraded gaps. " * 6
                ),
            }
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": "delegated_output_contract_repair",
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return repaired typed evidence",
                    "tools": ["web_search"],
                    "required_result_fields": ["study_title"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertTrue(result["retryable"])
        self.assertEqual(
            "agent_contract_noncompliance",
            result["failure_class"],
        )
        self.assertEqual(
            "delegated_output_contract_repair",
            result["terminal_reason"],
        )
        self.assertEqual(["web_search"], result["tool_audit"]["successful_tools"])
        self.assertIn("RESULT_FIELDS_JSON", result["error"])
        persist_result.assert_not_called()

    async def test_predispatch_output_repair_failure_remains_retryable(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Evidence\n"
                    "The bounded replacement is still missing its typed ledger, "
                    "but no tool handler was entered during this child run. " * 6
                ),
            }
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": "delegated_output_contract_repair",
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return repaired typed evidence",
                    "required_result_fields": ["study_title"],
                },
                _context(),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertTrue(result["retryable"])
        self.assertEqual(
            "agent_contract_noncompliance",
            result["failure_class"],
        )
        self.assertEqual(
            "delegated_output_contract_failed",
            result["terminal_reason"],
        )
        persist_result.assert_not_called()

    async def test_recovery_pseudo_tool_without_mutation_is_retryable(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Evidence\n"
                    "The bounded evidence result preserves source provenance and "
                    "marks the unavailable endpoint as a WARN/degraded gap. " * 6
                    + "\n<tool_call>execute_code>\n"
                    + '{"code":"print(\'not executed\')"}\n'
                    + "</execute_code>"
                ),
            }
            yield {
                "type": "agent_event",
                "event_type": "run.completed",
                "payload": {
                    "finish_reason": "stop",
                    "terminal_reason": (
                        "post_dispatch_stream_recovery_synthesis"
                    ),
                },
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return final bounded evidence",
                    "tools": ["web_search"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()
        self.assertTrue(result["retryable"])
        self.assertEqual(
            "agent_contract_noncompliance",
            result["failure_class"],
        )
        self.assertEqual(
            "post_dispatch_stream_recovery_synthesis",
            result["terminal_reason"],
        )
        self.assertIn("raw pseudo-tool protocol markup", result["error"])

    async def test_length_partial_with_closing_argument_protocol_is_not_persisted(self):
        content = (
            "# Evidence\n"
            "PASS: The bounded evidence review records verified findings, explicit "
            "provenance, validation status, and all unavailable-source gaps. " * 5
            + "Now serializing the result.<tool_call>execute_code\n"
            "code\n"
            + ("print('not actually executed')\n" * 300)
            + "\n"
            "</arg_value>language:python"
        )
        self.assertGreater(len(content), 200)
        self.assertGreater(
            content.index("</arg_value>") - content.index("<tool_call>"),
            8192,
        )
        protocol_audit = _raw_pseudo_tool_protocol_audit(content, [])
        self.assertEqual(
            content.index("<tool_call>"),
            protocol_audit["raw_protocol_first_offset"],
        )
        self.assertEqual(
            content.index("<tool_call>"),
            protocol_audit["clean_prefix_chars"],
        )
        self.assertGreater(protocol_audit["raw_protocol_span_chars"], 8192)

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {"type": "delta", "content": content}
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": (
                        "Delegated model hit its output limit; returning the bounded "
                        "partial child payload for outer contract validation."
                    ),
                    "finish_reason": "length",
                    "terminal_reason": "model_hit_max_output_tokens",
                    "partial_content_chars": len(content),
                },
            }
            yield {
                "type": "error",
                "msg": (
                    "Delegated model hit its output limit; returning the bounded "
                    "partial child payload for outer contract validation."
                ),
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/must-not-exist.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return final bounded evidence",
                    "tools": ["web_search"],
                    "required_result_fields": [],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["result_path"])
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["result_excerpt"], "")
        self.assertEqual(result["reasoning_summary"], "")
        persist_result.assert_not_called()
        self.assertTrue(result["result_field_audit"]["footer_valid"])
        self.assertEqual(result["result_field_audit"]["missing"], [])
        self.assertEqual(result["output_protocol_audit"]["detected_count"], 1)
        self.assertEqual(
            result["output_protocol_audit"]["unsupported_tool_names"],
            ["execute_code"],
        )
        self.assertIsNone(result["runtime_warning"])

    async def test_length_partial_with_named_unclosed_pseudo_call_is_not_persisted(self):
        # Real provider shape: the raw envelope names an executable capability,
        # then starts a fenced code payload but truncates before every protocol
        # argument/closing tag.  The fenced payload is documentation-safe in
        # isolation; the named envelope immediately before it is not.
        content = (
            "# Evidence\n"
            "PASS: The bounded review records verified findings, provenance, "
            "validation status, and explicit evidence gaps for every source. " * 5
            + "\n<tool_call>execute_code\n"
            "```python\n"
            "from pathlib import Path\n"
            "print(Path('report.md').read_text())\n"
            "```\n"
        )
        self.assertGreater(len(content), 200)

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {"type": "delta", "content": content}
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": (
                        "Delegated model hit its output limit; returning the bounded "
                        "partial child payload for outer contract validation."
                    ),
                    "finish_reason": "length",
                    "terminal_reason": "model_hit_max_output_tokens",
                    "partial_content_chars": len(content),
                },
            }
            yield {
                "type": "error",
                "msg": (
                    "Delegated model hit its output limit; returning the bounded "
                    "partial child payload for outer contract validation."
                ),
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/must-not-exist.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return final bounded evidence",
                    "tools": ["web_search"],
                    "required_result_fields": [],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["result_path"])
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["result_excerpt"], "")
        self.assertEqual(result["reasoning_summary"], "")
        persist_result.assert_not_called()
        self.assertEqual(result["output_protocol_audit"]["detected_count"], 1)
        self.assertEqual(
            result["output_protocol_audit"]["unsupported_tool_names"],
            ["execute_code"],
        )
        self.assertIsNone(result["runtime_warning"])

    async def test_markdown_tool_markup_examples_are_not_pseudo_calls(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Evidence\n"
                    "PASS: The report documents provider protocol behavior without "
                    "requesting another action. Provenance and verification are complete.\n\n"
                    "```xml\n"
                    "<tool_call>execute_code>\n{\"code\":\"example\"}\n</execute_code>\n"
                    "```\n\n"
                    "The inline literal `<tool_call>execute_code></execute_code>` is an "
                    "example only. The phrase <tool_call> in this discussion names a "
                    "protocol token but does not initiate a call.\n\n"
                    "    <tool_call>execute_code></execute_code>\n"
                    "The findings, evidence gaps, and blockers are fully accounted for. " * 3
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_protocol_docs.md",
            ),
        ):
            result = await _run_child(
                {"goal": "document protocol behavior", "tools": ["web_search"]},
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_protocol_audit"]["detected_count"], 0)
        self.assertEqual(
            result["output_protocol_audit"]["unsupported_tool_names"],
            [],
        )

    async def test_invalid_persistence_receipt_fails_without_reusable_body(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Evidence\n"
                    "PASS: The completed evidence review contains verified findings, "
                    "provenance, explicit gaps, and blockers for downstream reuse. " * 5
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="debug/not-a-reusable-result.md",
            ) as persist_result,
        ):
            result = await _run_child(
                {"goal": "return validated evidence", "tools": ["web_search"]},
                _context("web_search"),
                0,
            )

        persist_result.assert_called_once()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["result_path"])
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["result_excerpt"], "")
        self.assertIn("could not be persisted", result["error"])
        self.assertEqual(result["failure_class"], "agent_contract_noncompliance")
        self.assertTrue(result["retryable"])

    async def test_prior_same_name_tool_audit_does_not_authorize_raw_markup(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _tool_started(
                "execute_code",
                "real-code-call",
                code="print('executed')",
            )
            yield _tool_completed("execute_code", "real-code-call")
            yield {
                "type": "delta",
                "content": (
                    "# Evidence\n"
                    "PASS: A real structured execution completed and its audited result "
                    "supports this report. Provenance and verification are recorded. " * 4
                    + "\n<tool_call>execute_code>\n"
                    + '{"code":"print(\'executed\')"}\n'
                    + "</execute_code>"
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        persist = MagicMock(return_value="results/delegate_audited_tool.md")
        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                persist,
            ),
        ):
            result = await _run_child(
                {"goal": "return audited execution evidence", "tools": ["execute_code"]},
                _context("execute_code"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_class"], "agent_contract_noncompliance")
        self.assertEqual(
            result["output_protocol_audit"]["supported_tool_names"],
            [],
        )
        self.assertEqual(
            result["output_protocol_audit"]["unsupported_tool_names"],
            ["execute_code"],
        )
        self.assertEqual(
            result["output_protocol_audit"]["completed_name_overlap"],
            ["execute_code"],
        )
        persist.assert_not_called()

    def test_lone_documentation_marker_is_not_executable_protocol_shape(self):
        audit = _raw_pseudo_tool_protocol_audit(
            "# Notes\n<tool_call> is the provider marker discussed here.",
            [],
        )
        self.assertEqual(audit["detected_count"], 0)
        self.assertEqual(audit["unknown_unsupported_count"], 0)

    def test_explicit_named_unclosed_pseudo_call_shapes_are_rejected(self):
        samples = (
            '<tool_call name="execute_code">',
            "<tool_call><name>execute_code</name>",
            '<tool_call>{"name":"execute_code"}',
            "<tool_call>execute_code\n```python\nprint('example')\n```",
            '<tool_call>read_file\\":{\\"path\\":\\"result.md\\"}',
        )
        for content in samples:
            with self.subTest(content=content):
                audit = _raw_pseudo_tool_protocol_audit(content, [])
                self.assertEqual(audit["detected_count"], 1)
                self.assertEqual(
                    audit["unsupported_tool_names"],
                    [
                        "read_file"
                        if "read_file" in content
                        else "execute_code"
                    ],
                )

    async def test_all_empty_typed_ledger_without_result_body_is_rejected(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": 'RESULT_FIELDS_JSON: {"rows":[]}',
            }
            yield {"type": "done", "finish_reason": "stop"}

        persist = MagicMock(return_value="results/delegate_empty_rows.md")
        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                persist,
            ),
        ):
            result = await _run_child(
                {
                    "goal": "return the bounded query rows",
                    "required_result_fields": ["rows"],
                    "required_result_schema": {
                        "rows": {"type": "array"},
                    },
                },
                _context(),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("semantically empty", result["error"])
        self.assertTrue(
            result["result_shape"]["typed_result_semantic_audit"][
                "all_fields_structurally_empty"
            ]
        )
        persist.assert_not_called()

    async def test_empty_typed_collection_with_explicit_zero_result_is_valid(self):
        body = (
            "The bounded registry query completed and returned zero matching "
            "rows. Provenance: registry receipt R-0."
        )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": body + '\nRESULT_FIELDS_JSON: {"rows":[]}',
            }
            yield {"type": "done", "finish_reason": "stop"}

        persist = MagicMock(return_value="results/delegate_zero_rows.md")
        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                persist,
            ),
        ):
            result = await _run_child(
                {
                    "goal": "return the bounded query rows",
                    "required_result_fields": ["rows"],
                    "required_result_schema": {
                        "rows": {"type": "array"},
                    },
                },
                _context(),
                0,
            )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(
            result["result_shape"]["typed_result_semantic_audit"][
                "substantive_body"
            ]
        )
        self.assertTrue(
            result["result_shape"]["typed_result_semantic_audit"][
                "empty_ledger_justified"
            ]
        )
        persist.assert_called_once()

    async def test_empty_typed_collection_with_unrelated_body_is_rejected(self):
        body = (
            "# Findings\nThe retained evidence supports a material finding.\n"
            "Provenance: bounded registry receipt R-1."
        )

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": body + '\nRESULT_FIELDS_JSON: {"rows":[]}',
            }
            yield {"type": "done", "finish_reason": "stop"}

        persist = MagicMock(return_value="results/delegate_empty_rows.md")
        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                persist,
            ),
        ):
            result = await _run_child(
                {
                    "goal": "return the bounded query rows",
                    "required_result_fields": ["rows"],
                    "required_result_schema": {
                        "rows": {"type": "array"},
                    },
                },
                _context(),
                0,
            )

        self.assertEqual(result["status"], "error")
        semantic = result["result_shape"]["typed_result_semantic_audit"]
        self.assertTrue(semantic["substantive_body"])
        self.assertFalse(semantic["empty_ledger_justified"])
        persist.assert_not_called()

    def test_all_provider_stream_corruption_reasons_share_one_failure_class(self):
        for reason in (
            "provider_tool_stream_corrupt",
            "provider_tool_stream_corrupt_after_content",
            "provider_tool_stream_corrupt_after_repair",
            "provider_tool_stream_repair_not_emitted",
            "provider_tool_stream_repair_call_count_mismatch",
            "provider_tool_stream_post_dispatch_synthesis_failed",
        ):
            with self.subTest(reason=reason):
                conservative = _child_failure_fields(
                    "Provider returned a corrupt tool-call stream.",
                    reason,
                )
                self.assertEqual(
                    conservative["failure_class"],
                    "provider_protocol",
                )
                self.assertFalse(conservative["retryable"])

                proven_side_effect_free = _child_failure_fields(
                    "Provider returned a corrupt tool-call stream.",
                    reason,
                    retryable=True,
                )
                self.assertEqual(
                    proven_side_effect_free["failure_class"],
                    "provider_protocol",
                )
                self.assertTrue(proven_side_effect_free["retryable"])

    async def test_side_effect_free_provider_corruption_allows_parent_retry(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Second corrupt batch; no tool was dispatched.",
                    "finish_reason": (
                        "provider_tool_stream_corrupt_after_repair"
                    ),
                },
            }
            yield {
                "type": "error",
                "msg": "Second corrupt batch; no tool was dispatched.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {"goal": "return bounded evidence", "tools": ["web_search"]},
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_class"], "provider_protocol")
        self.assertTrue(result["retryable"])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_preflight_reject_is_not_dispatch_or_capability_attempt(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            started = _tool_started(
                "web_search",
                "preflight-rejected",
                query="bounded evidence",
            )
            started["payload"]["preflight_pending"] = True
            yield started
            yield {
                "type": "agent_event",
                "event_type": "tool.failed",
                "tool_name": "web_search",
                "tool_call_id": "preflight-rejected",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "preflight-rejected",
                    "outcome": "error",
                    "actual_dispatch_attempted": False,
                },
            }
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Second corrupt batch; no handler was entered.",
                    "finish_reason": (
                        "provider_tool_stream_corrupt_after_repair"
                    ),
                },
            }
            yield {
                "type": "error",
                "msg": "Second corrupt batch; no handler was entered.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return bounded evidence",
                    "tools": ["web_search"],
                    "required_capability_tools": ["web_search"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_class"], "provider_protocol")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["tool_audit"]["attempted_tools"], [])
        self.assertEqual(result["tool_audit"]["successful_tools"], [])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_new_preflight_terminal_without_receipt_is_not_dispatch(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            started = _tool_started(
                "web_search",
                "missing-receipt",
                query="bounded evidence",
            )
            started["payload"]["preflight_pending"] = True
            yield started
            yield {
                "type": "agent_event",
                "event_type": "tool.failed",
                "tool_name": "web_search",
                "tool_call_id": "missing-receipt",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "missing-receipt",
                    "outcome": "error",
                },
            }
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Second corrupt batch; dispatch was not proven.",
                    "finish_reason": (
                        "provider_tool_stream_corrupt_after_repair"
                    ),
                },
            }
            yield {
                "type": "error",
                "msg": "Second corrupt batch; dispatch was not proven.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return bounded evidence",
                    "tools": ["web_search"],
                    "required_capability_tools": ["web_search"],
                },
                _context("web_search"),
                0,
            )

        self.assertEqual(result["failure_class"], "provider_protocol")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["tool_audit"]["attempted_tools"], [])
        self.assertEqual(result["tool_audit"]["successful_tools"], [])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_dispatch_started_without_terminal_blocks_parent_retry(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            started = _tool_started(
                "web_search",
                "handler-entered",
                query="bounded evidence",
            )
            started["payload"]["preflight_pending"] = True
            yield started
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "handler-entered",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "handler-entered",
                    "actual_dispatch_attempted": True,
                },
            }
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Provider failed after handler entry.",
                    "finish_reason": (
                        "provider_tool_stream_corrupt_after_repair"
                    ),
                },
            }
            yield {
                "type": "error",
                "msg": "Provider failed after handler entry.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {"goal": "return bounded evidence", "tools": ["web_search"]},
                _context("web_search"),
                0,
            )

        self.assertEqual(result["failure_class"], "provider_protocol")
        self.assertFalse(result["retryable"])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_reused_call_id_does_not_inherit_old_dispatch_boundary(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            first_started = _tool_started(
                "web_search",
                "reused-id",
                query="bounded evidence",
            )
            first_started["payload"]["preflight_pending"] = True
            yield first_started
            yield {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": "web_search",
                "tool_call_id": "reused-id",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "reused-id",
                    "actual_dispatch_attempted": True,
                },
            }
            first_completed = _tool_completed("web_search", "reused-id")
            first_completed["payload"]["actual_dispatch_attempted"] = True
            yield first_completed

            second_started = _tool_started(
                "web_extract",
                "reused-id",
                url="https://example.invalid",
            )
            second_started["payload"]["preflight_pending"] = True
            yield second_started
            yield {
                "type": "agent_event",
                "event_type": "tool.failed",
                "tool_name": "web_extract",
                "tool_call_id": "reused-id",
                "payload": {
                    "tool_name": "web_extract",
                    "tool_call_id": "reused-id",
                    "outcome": "error",
                    "actual_dispatch_attempted": False,
                },
            }
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Provider stream failed after bounded activity.",
                    "finish_reason": (
                        "provider_tool_stream_corrupt_after_repair"
                    ),
                },
            }
            yield {
                "type": "error",
                "msg": "Provider stream failed after bounded activity.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "return bounded evidence",
                    "tools": ["web_search", "web_extract"],
                },
                _context("web_search", "web_extract"),
                0,
            )

        self.assertFalse(result["retryable"])
        self.assertEqual(
            result["tool_audit"]["attempted_tools"],
            ["web_search"],
        )
        self.assertEqual(
            result["tool_audit"]["successful_tools"],
            ["web_search"],
        )
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_side_effect_free_repair_not_emitted_overrides_retry_hint(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Repair turn emitted no valid tool batch.",
                    "finish_reason": "provider_tool_stream_repair_not_emitted",
                    # Only the whole-child dispatch audit is authoritative for
                    # provider-protocol classification and resampling safety.
                    "failure_class": "terminal_runtime",
                    "retryable": False,
                },
            }
            yield {
                "type": "error",
                "msg": "Repair turn emitted no valid tool batch.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {"goal": "return bounded evidence", "tools": ["web_search"]},
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_class"], "provider_protocol")
        self.assertTrue(result["retryable"])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_side_effect_free_repair_call_count_mismatch_is_retryable(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Repair turn emitted multiple tool calls.",
                    "finish_reason": (
                        "provider_tool_stream_repair_call_count_mismatch"
                    ),
                    "failure_class": "terminal_runtime",
                    "retryable": False,
                },
            }
            yield {
                "type": "error",
                "msg": "Repair turn emitted multiple tool calls.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {"goal": "return bounded evidence", "tools": ["web_search"]},
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_class"], "provider_protocol")
        self.assertTrue(result["retryable"])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_provider_corruption_after_tool_start_is_not_retryable(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _tool_started(
                "web_search",
                "possibly-dispatched",
                query="bounded evidence",
            )
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Corrupt batch after prior child activity.",
                    "finish_reason": (
                        "provider_tool_stream_corrupt_after_repair"
                    ),
                },
            }
            yield {
                "type": "error",
                "msg": "Corrupt batch after prior child activity.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {"goal": "return bounded evidence", "tools": ["web_search"]},
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_class"], "provider_protocol")
        self.assertFalse(result["retryable"])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_post_dispatch_synthesis_provider_failure_is_protocol_error(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _tool_started(
                "web_search",
                "actually-dispatched",
                query="bounded evidence",
            )
            completed = _tool_completed(
                "web_search",
                "actually-dispatched",
            )
            completed["payload"]["actual_dispatch_attempted"] = True
            yield completed
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "No-tool recovery synthesis was truncated.",
                    "finish_reason": (
                        "provider_tool_stream_post_dispatch_synthesis_failed"
                    ),
                },
            }
            yield {
                "type": "error",
                "msg": "No-tool recovery synthesis was truncated.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {"goal": "return bounded evidence", "tools": ["web_search"]},
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_class"], "provider_protocol")
        self.assertFalse(result["retryable"])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_repair_not_emitted_after_any_tool_audit_is_not_retryable(self):
        audit_events = {
            "started": _tool_started(
                "web_search",
                "possibly-dispatched",
                query="bounded evidence",
            ),
            "completed": _tool_completed(
                "web_search",
                "possibly-dispatched",
            ),
            "failed": {
                "type": "agent_event",
                "event_type": "tool.failed",
                "tool_name": "web_search",
                "tool_call_id": "possibly-dispatched",
                "payload": {
                    "tool_name": "web_search",
                    "tool_call_id": "possibly-dispatched",
                    "outcome": "error",
                },
            },
        }

        for event_kind, audit_event in audit_events.items():
            with self.subTest(event_kind=event_kind):
                async def fake_run_stream(model_id, messages, tools, **kwargs):
                    yield audit_event
                    yield {
                        "type": "agent_event",
                        "event_type": "run.failed",
                        "payload": {
                            "error": "Repair turn emitted no valid tool batch.",
                            "finish_reason": (
                                "provider_tool_stream_repair_not_emitted"
                            ),
                            # A lower layer cannot authorize a whole-child
                            # retry after an audited dispatch boundary.
                            "retryable": True,
                        },
                    }
                    yield {
                        "type": "error",
                        "msg": "Repair turn emitted no valid tool batch.",
                    }

                with (
                    patch("agent_loop.run_stream", fake_run_stream),
                    patch(
                        "tools.delegation.persist_result_for_history"
                    ) as persist_result,
                ):
                    result = await _run_child(
                        {
                            "goal": "return bounded evidence",
                            "tools": ["web_search"],
                        },
                        _context("web_search"),
                        0,
                    )

                self.assertEqual(result["status"], "error")
                self.assertEqual(result["failure_class"], "provider_protocol")
                self.assertFalse(result["retryable"])
                self.assertIsNone(result["result_path"])
                persist_result.assert_not_called()

    async def test_repair_call_count_mismatch_after_dispatch_is_not_retryable(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _tool_started(
                "web_search",
                "possibly-dispatched",
                query="bounded evidence",
            )
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": "Repair turn emitted multiple tool calls.",
                    "finish_reason": (
                        "provider_tool_stream_repair_call_count_mismatch"
                    ),
                    "failure_class": "terminal_runtime",
                    "retryable": True,
                },
            }
            yield {
                "type": "error",
                "msg": "Repair turn emitted multiple tool calls.",
            }

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {"goal": "return bounded evidence", "tools": ["web_search"]},
                _context("web_search"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_class"], "provider_protocol")
        self.assertFalse(result["retryable"])
        self.assertIsNone(result["result_path"])
        persist_result.assert_not_called()

    async def test_artifact_text_ledger_without_receipts_is_rejected(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "# Completion ledger\n"
                    "01_summary.md — PASS: written successfully. "
                    "The package is complete and verified. " * 8
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch("tools.delegation.get_workspace") as get_workspace,
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_artifacts.md",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "synthesize artifacts",
                    "step_type": "artifact_synthesis",
                    "required_output_ids": ["01_summary.md"],
                    "tools": ["write_file"],
                },
                _context("write_file"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("no verified successful", result["error"])
        self.assertEqual(result["artifact_receipts"], [])
        get_workspace.assert_not_called()

    async def test_artifact_receipts_must_match_real_declared_workspace_files(self):
        observed_events: list[dict] = []

        async def capture(event):
            observed_events.append(dict(event))

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            async def fake_run_stream(model_id, messages, tools, **kwargs):
                for call_id, filepath, body in (
                    ("write-1", "01_summary.md", "# Summary\n" + "evidence\n" * 20),
                    ("write-2", "alpha_details.md", "# Details\n" + "facts\n" * 20),
                ):
                    yield _tool_started(
                        "write_file",
                        call_id,
                        filepath=filepath,
                        content=body,
                    )
                    (workspace / filepath).write_text(body, encoding="utf-8")
                    yield _tool_completed("write_file", call_id)
                yield {
                    "type": "delta",
                    "content": (
                        "# Completion ledger\n"
                        "Verified workspace writes are recorded for 01_summary.md and "
                        "alpha_details.md. Both reports contain substantive evidence and "
                        "provenance, and the declared package is ready for downstream checks. " * 4
                    ),
                }
                yield {"type": "done", "finish_reason": "stop"}

            with (
                patch("agent_loop.run_stream", fake_run_stream),
                patch("tools.delegation.get_workspace", return_value=workspace),
                patch(
                    "tools.delegation.persist_result_for_history",
                    return_value="results/delegate_artifacts.md",
                ),
            ):
                result = await _run_child(
                    {
                        "goal": "synthesize artifacts",
                        "step_type": "artifact_synthesis",
                        "required_output_ids": [
                            "01_summary.md",
                            "{PROJECT}_details.md",
                        ],
                        "tools": ["write_file"],
                    },
                    _context("write_file", event_sink=capture),
                    0,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            [receipt["path"] for receipt in result["artifact_receipts"]],
            ["01_summary.md", "alpha_details.md"],
        )
        self.assertTrue(all(
            receipt["source_tool"] == "write_file"
            for receipt in result["artifact_receipts"]
        ))
        terminal = observed_events[-1]
        self.assertEqual("run.completed", terminal["event_type"])
        self.assertEqual(
            result["artifact_manifest"],
            terminal["payload"]["artifact_manifest"],
        )
        self.assertEqual(
            2, terminal["payload"]["artifact_manifest"]["receipt_count"]
        )

    async def test_run_skill_process_raw_artifact_enters_terminal_manifest(self):
        for operation in ("sync", "close"):
            with self.subTest(operation=operation):
                observed_events: list[dict] = []

                async def capture(event):
                    observed_events.append(dict(event))

                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    body = "# Process report\n" + "verified evidence\n" * 20
                    artifact_path = workspace / "process_report.md"
                    artifact_path.write_text(body, encoding="utf-8")
                    raw_result = json.dumps({
                        "status": "success",
                        "operation": operation,
                        "artifacts": [{
                            "path": "process_report.md",
                            "size_bytes": artifact_path.stat().st_size,
                            "sha256": hashlib.sha256(
                                body.encode("utf-8")
                            ).hexdigest(),
                        }],
                    })
                    emitted_artifacts = _artifact_payloads_from_tool_result(
                        "run_skill_process",
                        raw_result,
                    )
                    self.assertEqual(1, len(emitted_artifacts), raw_result)

                    async def fake_run_stream(
                        model_id, messages, tools, **kwargs
                    ):
                        call_id = f"process-{operation}"
                        yield _tool_started(
                            "run_skill_process",
                            call_id,
                            operation=operation,
                            process_id="process-1",
                        )
                        yield {
                            "type": "agent_event",
                            "event_type": "tool.completed",
                            "tool_name": "run_skill_process",
                            "tool_call_id": call_id,
                            "payload": {
                                "tool_name": "run_skill_process",
                                "tool_call_id": call_id,
                                "outcome": "success",
                                "artifacts": emitted_artifacts,
                            },
                        }
                        yield {
                            "type": "delta",
                            "content": (
                                "# Completion ledger\n"
                                "The persistent process was synchronized; "
                                "process_report.md contains the verified "
                                "result and provenance. " * 6
                            ),
                        }
                        yield {"type": "done", "finish_reason": "stop"}

                    with (
                        patch("agent_loop.run_stream", fake_run_stream),
                        patch(
                            "tools.delegation.get_workspace",
                            return_value=workspace,
                        ),
                        patch(
                            "tools.delegation.persist_result_for_history",
                            return_value=(
                                "results/delegate_process_artifacts.md"
                            ),
                        ),
                    ):
                        result = await _run_child(
                            {
                                "goal": (
                                    f"{operation} the process and synthesize "
                                    "its artifact"
                                ),
                                "step_type": "artifact_synthesis",
                                "required_output_ids": ["process_report.md"],
                                "tools": ["run_skill_process"],
                            },
                            _context(
                                "run_skill_process",
                                event_sink=capture,
                            ),
                            0,
                        )

                self.assertEqual(
                    "completed", result["status"], result.get("error")
                )
                self.assertEqual(
                    ["process_report.md"],
                    [item["path"] for item in result["artifact_receipts"]],
                )
                self.assertEqual(
                    "run_skill_process",
                    result["artifact_receipts"][0]["source_tool"],
                )
                self.assertEqual(
                    result["artifact_manifest"],
                    observed_events[-1]["payload"]["artifact_manifest"],
                )

    async def test_artifact_receipt_uses_preflight_normalized_audit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            async def fake_run_stream(model_id, messages, tools, **kwargs):
                call_id = "write-alias"
                yield {
                    "type": "agent_event",
                    "event_type": "tool.started",
                    "tool_name": "write_file",
                    "tool_call_id": call_id,
                    "payload": {
                        "tool_name": "write_file",
                        "tool_call_id": call_id,
                        "args_compacted": {
                            "path": "alias_report.md",
                            "content_omitted": {
                                "_chatds_argument_omitted": True,
                            },
                        },
                        "args_are_dispatch_payload": False,
                        "preflight_pending": True,
                    },
                }
                yield {
                    "type": "agent_event",
                    "event_type": "tool.dispatch_started",
                    "tool_name": "write_file",
                    "tool_call_id": call_id,
                    "payload": {
                        "tool_name": "write_file",
                        "tool_call_id": call_id,
                        "audit_args": {"filepath": "alias_report.md"},
                        "audit_args_are_dispatch_derived": True,
                    },
                }
                (workspace / "alias_report.md").write_text(
                    "# Alias report\n" + "evidence\n" * 20,
                    encoding="utf-8",
                )
                yield _tool_completed("write_file", call_id)
                yield {
                    "type": "delta",
                    "content": (
                        "# Completion ledger\n"
                        "alias_report.md was written from normalized dispatch "
                        "arguments with verified evidence and provenance. " * 6
                    ),
                }
                yield {"type": "done", "finish_reason": "stop"}

            with (
                patch("agent_loop.run_stream", fake_run_stream),
                patch("tools.delegation.get_workspace", return_value=workspace),
                patch(
                    "tools.delegation.persist_result_for_history",
                    return_value="results/delegate_alias.md",
                ),
            ):
                result = await _run_child(
                    {
                        "goal": "synthesize one aliased artifact",
                        "step_type": "artifact_synthesis",
                        "required_output_ids": ["alias_report.md"],
                        "tools": ["write_file"],
                    },
                    _context("write_file"),
                    0,
                )

        self.assertEqual("completed", result["status"], result.get("error"))
        self.assertEqual(
            ["alias_report.md"],
            [item["path"] for item in result["artifact_receipts"]],
        )

    async def test_success_event_without_actual_file_is_not_a_receipt(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _tool_started(
                "write_file",
                "write-missing",
                filepath="01_summary.md",
                content="claimed content",
            )
            yield _tool_completed("write_file", "write-missing")
            yield {
                "type": "delta",
                "content": "# Completion ledger\n01_summary.md — PASS: claimed written. " * 8,
            }
            yield {"type": "done", "finish_reason": "stop"}

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("agent_loop.run_stream", fake_run_stream),
                patch("tools.delegation.get_workspace", return_value=Path(tmp)),
                patch(
                    "tools.delegation.persist_result_for_history",
                    return_value="results/delegate_artifacts.md",
                ),
            ):
                result = await _run_child(
                    {
                        "goal": "synthesize artifacts",
                        "step_type": "artifact_synthesis",
                        "required_output_ids": ["01_summary.md"],
                        "tools": ["write_file"],
                    },
                    _context("write_file"),
                    0,
                )

        self.assertEqual(result["status"], "error")
        self.assertIn("no verified successful", result["error"])
        self.assertEqual(result["artifact_receipts"], [])

    async def test_nested_receipt_does_not_satisfy_root_artifact_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            nested = workspace / "nested"
            nested.mkdir()

            async def fake_run_stream(model_id, messages, tools, **kwargs):
                body = "# Summary\n" + "verified evidence\n" * 20
                yield _tool_started(
                    "write_file",
                    "write-nested",
                    filepath="nested/01_summary.md",
                    content=body,
                )
                (nested / "01_summary.md").write_text(body, encoding="utf-8")
                yield _tool_completed("write_file", "write-nested")
                yield {
                    "type": "delta",
                    "content": (
                        "# Completion ledger\n"
                        "nested/01_summary.md was written with verified evidence and "
                        "provenance, but no root artifact was created. " * 6
                    ),
                }
                yield {"type": "done", "finish_reason": "stop"}

            with (
                patch("agent_loop.run_stream", fake_run_stream),
                patch("tools.delegation.get_workspace", return_value=workspace),
                patch(
                    "tools.delegation.persist_result_for_history",
                    return_value="results/delegate_artifacts.md",
                ),
            ):
                result = await _run_child(
                    {
                        "goal": "synthesize artifacts",
                        "step_type": "artifact_synthesis",
                        "required_output_ids": ["01_summary.md"],
                        "tools": ["write_file"],
                    },
                    _context("write_file"),
                    0,
                )

        self.assertEqual(result["status"], "error")
        self.assertIn(
            "omitted verified artifact-production receipts",
            result["error"],
        )
        self.assertEqual(
            [receipt["path"] for receipt in result["artifact_receipts"]],
            ["nested/01_summary.md"],
        )

    async def test_multiple_receipts_for_one_placeholder_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            async def fake_run_stream(model_id, messages, tools, **kwargs):
                for index, filepath in enumerate(
                    ("alpha_details.md", "beta_details.md"),
                    start=1,
                ):
                    body = "# Details\n" + "verified evidence\n" * 20
                    yield _tool_started(
                        "write_file",
                        f"write-{index}",
                        filepath=filepath,
                        content=body,
                    )
                    (workspace / filepath).write_text(body, encoding="utf-8")
                    yield _tool_completed("write_file", f"write-{index}")
                yield {
                    "type": "delta",
                    "content": "# Ledger\nBoth candidate details files were written. " * 12,
                }
                yield {"type": "done", "finish_reason": "stop"}

            with (
                patch("agent_loop.run_stream", fake_run_stream),
                patch("tools.delegation.get_workspace", return_value=workspace),
                patch(
                    "tools.delegation.persist_result_for_history",
                    return_value="results/delegate_artifacts.md",
                ),
            ):
                result = await _run_child(
                    {
                        "goal": "synthesize one artifact",
                        "step_type": "artifact_synthesis",
                        "required_output_ids": ["{PROJECT}_details.md"],
                        "tools": ["write_file"],
                    },
                    _context("write_file"),
                    0,
                )

        self.assertEqual("error", result["status"])
        self.assertIn("ambiguous", result["error"])
        self.assertEqual(
            ["alpha_details.md", "beta_details.md"],
            [receipt["path"] for receipt in result["artifact_receipts"]],
        )


if __name__ == "__main__":
    unittest.main()
