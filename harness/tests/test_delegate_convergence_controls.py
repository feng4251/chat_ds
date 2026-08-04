import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import (
    DirectToolExposure,
    _deterministic_http_client_failure,
    _delegated_success_no_progress_candidate,
    _workspace_debug_record,
    run_stream,
)
from knowledge_gate_runtime import (
    KNOWLEDGE_GATE_DECISION_TOOL_NAME,
    canonical_json_sha256,
    validate_knowledge_gate_decisions,
)
from retrieval_completeness import build_http_retrieval_receipt
from tool_call_stream import (
    AssembledStreamToolCall,
    ToolCallStreamAssembly,
)
from tools.registry import ToolPreflightResult, registry as native_tool_registry
from tools.context import ToolContext
from tools.delegation import _exact_knowledge_gate_candidate_grants
from tools.isolated_skill_executor import compute_skill_package_digest
from tools.tool_result_reader import READ_TOOL_RESULT_SCHEMA


def _tool_call_response(
    call_id: str = "call-search",
    *,
    tool_name: str = "web_search",
    arguments: dict | None = None,
) -> list[str]:
    if arguments is None:
        arguments = {"query": "bounded evidence"}
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(arguments),
                        },
                    }],
                },
                "finish_reason": None,
            }],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }),
        "data: [DONE]",
    ]


def _tool_calls_response(
    calls: list[tuple[str, str, dict]],
) -> list[str]:
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                        for index, (call_id, tool_name, arguments)
                        in enumerate(calls)
                    ],
                },
                "finish_reason": None,
            }],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }),
        "data: [DONE]",
    ]


def _stop_response(content: str) -> list[str]:
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {"content": content},
                "finish_reason": None,
            }],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "stop"}],
        }),
        "data: [DONE]",
    ]


def _reasoning_length_response(
    reasoning: str = "internal-chain " * 300,
    *,
    content: str = "",
) -> list[str]:
    delta = {"reasoning_content": reasoning}
    if content:
        delta["content"] = content
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": delta,
                "finish_reason": None,
            }],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "length"}],
        }),
        "data: [DONE]",
    ]


def _visible_length_response(content: str) -> list[str]:
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {"content": content},
                "finish_reason": None,
            }],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "length"}],
        }),
        "data: [DONE]",
    ]


def _partial_corrupt_tool_response(
    *,
    content: str = "retained setup text",
) -> list[str]:
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {
                    "content": content,
                    "reasoning_content": "discarded hidden setup reasoning",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-corrupt-early",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"unterminated',
                        },
                    }],
                },
                "finish_reason": None,
            }],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }),
        "data: [DONE]",
    ]


def _result_fields_footer(*fields: str, degraded: bool = False) -> str:
    ledger = {}
    for field in fields:
        if degraded:
            ledger[field] = {
                "status": "degraded",
                "reason": "authorized source unavailable",
                "provenance": "search attempt and fallback audit",
            }
        else:
            ledger[field] = {
                "status": "present",
                "value_summary": f"verified {field}",
                "provenance": "bounded tool result",
            }
    return "RESULT_FIELDS_JSON: " + json.dumps(
        ledger,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _result_fields_arguments(*fields: str, degraded: bool = False) -> dict:
    return json.loads(
        _result_fields_footer(*fields, degraded=degraded).split(": ", 1)[1]
    )


def _result_fields_submit_response(
    *fields: str,
    degraded: bool = False,
    call_id: str = "call-submit-result-fields",
) -> list[str]:
    return _tool_call_response(
        call_id,
        tool_name="submit_result_fields",
        arguments=_result_fields_arguments(*fields, degraded=degraded),
    )


def _http_success_result(
    url: str,
    response_body: str,
    *,
    request_number: int,
    body_truncated: bool = False,
    method: str = "GET",
    request_body: dict | None = None,
    max_chars: int = 40_000,
) -> str:
    returned_body = response_body[:max_chars]
    return json.dumps({
        "status": "success",
        "request_sent": True,
        "request_number": request_number,
        "url": url,
        "body": returned_body,
        "body_chars": len(returned_body),
        "body_truncated": body_truncated,
        "retrieval": build_http_retrieval_receipt(
            method=method,
            request_url=url,
            request_body=request_body,
            response_body=returned_body,
            body_truncated=body_truncated,
            response_bytes_read=len(response_body.encode("utf-8")),
            response_byte_limit=400_000,
            response_chars_returned=len(returned_body),
            response_char_limit=max_chars,
            request_number=request_number,
            request_run_hop_limit=16,
            request_elapsed_ms=5,
        ),
    })


class DelegateConvergenceControlTests(unittest.IsolatedAsyncioTestCase):
    provider = {
        "id": "mock-delegate-convergence",
        "base_url": "http://model.invalid/v1",
        "api_model": "mock-delegate-convergence",
        "api_key": "EMPTY",
        "protocol": "openai",
        "provider": "mock",
        "context_length": 64_000,
        "is_multimodal": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
    }
    web_schema = [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search for evidence.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }]
    fake_tool_names = ("fake_tool_a", "fake_tool_b", "fake_tool_c")
    fake_tool_schemas = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "Return bounded generic evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }
        for name in fake_tool_names
    ]
    http_schema = [{
        "type": "function",
        "function": {
            "name": "skill_http_get",
            "description": "Fetch one exact authorized HTTPS evidence page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100_000,
                    },
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    }]

    @staticmethod
    def _allow_fake_tool_preflight(name, args, _context=None, **_kwargs):
        return ToolPreflightResult(
            name=name,
            args=dict(args),
            semantic_args=dict(args),
        )

    def test_success_no_progress_candidate_is_delegated_only(self):
        common = {
            "outcome": "success",
            "tool_name": "evidence_tool",
            "available_tools": ["evidence_tool"],
            "artifact_progress": False,
            "workflow_state_changed": False,
        }
        self.assertTrue(_delegated_success_no_progress_candidate(
            delegated_subtask=True,
            **common,
        ))
        self.assertFalse(_delegated_success_no_progress_candidate(
            delegated_subtask=False,
            **common,
        ))

    def test_terminal_http_classifier_preserves_retryable_failures(self):
        base = {
            "status": "error",
            "request_sent": True,
            "error_code": "http_status_error",
        }
        for status in (400, 401, 403, 404, 422, 451):
            self.assertEqual(
                status,
                _deterministic_http_client_failure(
                    "skill_http_get", {**base, "http_status": status}
                )["http_status"],
            )
        for status in (408, 409, 425, 429, 500, 503):
            self.assertIsNone(_deterministic_http_client_failure(
                "skill_http_get", {**base, "http_status": status}
            ))
        self.assertIsNone(_deterministic_http_client_failure(
            "skill_http_get",
            {**base, "http_status": 404, "request_sent": False},
        ))
        self.assertIsNone(_deterministic_http_client_failure(
            "web_extract", {**base, "http_status": 404}
        ))

    def test_workspace_debug_renames_inner_terminal_candidate(self):
        record = _workspace_debug_record({
            "event_type": "run.completed",
            "run_id": "child",
            "payload": {
                "finish_reason": "stop",
                "provisional_terminal": True,
                "authoritative": False,
            },
        })

        self.assertEqual(
            "debug.agent_loop.terminal_candidate",
            record["event_type"],
        )
        self.assertEqual(
            "run.completed",
            record["payload"]["candidate_terminal_type"],
        )
        self.assertEqual(
            "inner_agent_loop_candidate",
            record["payload"]["lifecycle_scope"],
        )

    async def test_truncated_http_receipt_requires_exact_retry_before_completion(self):
        url = "https://api.vendor.test/search?q=evidence&pageSize=20"
        responses = [
            _tool_call_response(
                "call-http-truncated",
                tool_name="skill_http_get",
                arguments={"url": url, "max_chars": 10},
            ),
            _tool_call_response(
                "call-http-retry",
                tool_name="skill_http_get",
                arguments={"url": url, "max_chars": 100_000},
            ),
            _stop_response(
                "Evidence retrieval closed.\n"
                + _result_fields_footer("evidence")
            ),
        ]
        dispatch_results = [
            _http_success_result(
                url,
                '{"items":[1,2,3]}',
                request_number=1,
                body_truncated=True,
                max_chars=10,
            ),
            _http_success_result(
                url,
                '{"items":[1,2,3]}',
                request_number=2,
                max_chars=100_000,
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result="",
            dispatch_results=dispatch_results,
            tools=["skill_http_get"],
            schemas=self.http_schema,
            required_result_fields=["evidence"],
            required_capability_tools=["skill_http_get"],
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/search"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(2, dispatch_mock.await_count)
        self.assertEqual(
            [10, 100_000],
            [
                call.args[1].get("max_chars")
                for call in dispatch_mock.await_args_list
            ],
        )
        self.assertEqual("required", request_bodies[0].get("tool_choice"))
        self.assertEqual(
            {"enable_thinking": False},
            request_bodies[0].get("chat_template_kwargs"),
        )
        self.assertEqual("required", request_bodies[1].get("tool_choice"))
        self.assertEqual(
            {"enable_thinking": False},
            request_bodies[1].get("chat_template_kwargs"),
        )
        self.assertEqual(
            ["skill_http_get"],
            [
                item["function"]["name"]
                for item in request_bodies[1]["tools"]
            ],
        )
        receipts = [
            event["payload"]
            for event in events
            if event.get("event_type")
            == "debug.http_retrieval.receipt"
        ]
        self.assertEqual([1, 0], [
            item["open_chain_count"] for item in receipts
        ])
        self.assertFalse(any(
            event.get("event_type") == "run.failed"
            for event in events
        ))

    async def test_identical_terminal_http_4xx_is_blocked_before_replay(self):
        first_url = "https://api.vendor.test/records?query=portable"
        changed_url = first_url + "&page=2"
        responses = [
            _tool_call_response(
                "call-terminal-404",
                tool_name="skill_http_get",
                arguments={"url": first_url, "max_chars": 40_000},
            ),
            _tool_call_response(
                "call-terminal-404-replay",
                tool_name="skill_http_get",
                arguments={"url": first_url, "max_chars": 40_000},
            ),
            _tool_call_response(
                "call-changed-http-request",
                tool_name="skill_http_get",
                arguments={"url": changed_url, "max_chars": 40_000},
            ),
            _stop_response(
                "status: WARN\nEvidence: changed bounded request succeeded\n"
                "Gap: the original exact request returned HTTP 404"
            ),
        ]
        first_failure = json.dumps({
            "status": "error",
            "request_sent": True,
            "request_number": 1,
            "root_request_number": 1,
            "matched_skill": "evidence-api",
            "matched_prefix_sha256": hashlib.sha256(
                "https://api.vendor.test:443/records".encode("utf-8")
            ).hexdigest(),
            "url": first_url,
            "http_status": 404,
            "error_code": "http_status_error",
            "error": "Skill endpoint returned HTTP 404.",
        })
        changed_success = _http_success_result(
            changed_url,
            '{"items":[{"id":"portable"}]}',
            request_number=2,
        )

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result="",
            dispatch_results=[first_failure, changed_success],
            tools=["skill_http_get"],
            schemas=self.http_schema,
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/records"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(2, dispatch_mock.await_count)
        self.assertEqual(
            [first_url, changed_url],
            [call.args[1]["url"] for call in dispatch_mock.await_args_list],
        )
        terminal = [
            event["payload"] for event in events
            if event.get("event_type")
            == "debug.http.invocation_terminal"
        ]
        blocked = [
            event["payload"] for event in events
            if event.get("event_type")
            == "debug.http.invocation_replay_blocked"
        ]
        self.assertEqual(1, len(terminal))
        self.assertEqual(404, terminal[0]["http_status"])
        self.assertEqual(1, len(blocked))
        self.assertFalse(blocked[0]["actual_dispatch_attempted"])
        self.assertEqual(
            terminal[0]["argument_sha256"],
            blocked[0]["argument_sha256"],
        )
        self.assertIn(
            "deterministic_http_failure_replay",
            json.dumps(request_bodies[2]["messages"], ensure_ascii=False),
        )
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ), events)
        self.assertEqual(
            {"type": "done", "finish_reason": "stop"},
            events[-1],
        )

    async def test_http_body_spill_handle_is_granted_for_bounded_readback(self):
        url = "https://api.vendor.test/search?q=evidence"
        handle = "tool-result:skill_http_get_body_123.txt"
        full_body = '{"items":[{"text":"' + ("x" * 300) + '"}]}'
        visible = full_body[:100]
        receipt = build_http_retrieval_receipt(
            method="GET",
            request_url=url,
            request_body=None,
            response_body=visible,
            body_truncated=True,
            body_spilled_complete=True,
            wire_body_complete=True,
            pagination_scan_body=full_body,
            response_bytes_read=len(full_body.encode("utf-8")),
            response_byte_limit=400_000,
            response_chars_read=len(full_body),
            response_chars_returned=len(visible),
            response_char_limit=100,
            response_char_hard_limit=100_000,
            request_number=1,
            request_run_hop_limit=16,
            request_elapsed_ms=5,
        )
        http_result = json.dumps({
            "status": "success",
            "request_sent": True,
            "request_number": 1,
            "url": url,
            "body_result_handle": handle,
            "body": visible,
            "body_chars": len(visible),
            "body_truncated": True,
            "body_spilled_complete": True,
            "retrieval": receipt,
        })
        responses = [
            _tool_call_response(
                "call-http-spill",
                tool_name="skill_http_get",
                arguments={"url": url, "max_chars": 100},
            ),
            _tool_call_response(
                "call-read-spill",
                tool_name="read_tool_result",
                arguments={"handle": handle, "pattern": "text"},
            ),
            _stop_response(
                "Evidence readback completed.\n"
                + _result_fields_footer("evidence")
            ),
        ]
        schemas = [*self.http_schema, {
            "type": "function",
            "function": READ_TOOL_RESULT_SCHEMA,
        }]

        def schema_resolver(names):
            selected = set(names)
            return [
                schema
                for schema in schemas
                if schema["function"]["name"] in selected
            ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result="",
            dispatch_results=[
                http_result,
                json.dumps({
                    "status": "success",
                    "handle": handle,
                    "content": '"text":"' + ("x" * 20),
                    "start_offset": 10,
                    "end_offset": 39,
                    "total_chars": len(full_body),
                    "has_more_before": True,
                    "has_more_after": True,
                }),
            ],
            tools=["skill_http_get"],
            schemas=schemas,
            schema_resolver=schema_resolver,
            required_result_fields=["evidence"],
            required_capability_tools=["skill_http_get"],
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/search"
            )],
        )

        self.assertEqual(2, dispatch_mock.await_count)
        self.assertNotIn(
            "read_tool_result",
            {
                item["function"]["name"]
                for item in request_bodies[0]["tools"]
            },
        )
        second_context = dispatch_mock.await_args_list[1].kwargs["context"]
        self.assertIn(handle, second_context.allowed_tool_result_handles)
        self.assertIn(
            "read_tool_result",
            {
                item["function"]["name"]
                for item in request_bodies[1]["tools"]
            },
        )
        self.assertTrue(any(
            event.get("event_type") == "debug.tool.result_spill"
            and event.get("payload", {}).get("source")
            == "complete_http_body"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))

    async def test_spill_handle_does_not_reopen_tools_closed_footer_finalizer(self):
        """A prior opaque handle cannot pierce the terminal phase boundary."""

        url = "https://api.inventory.test/v1/records?q=archival"
        handle = "tool-result:skill_http_get_body_inventory.txt"
        full_body = '{"records":[{"note":"' + ("x" * 300) + '"}]}'
        visible = full_body[:100]
        receipt = build_http_retrieval_receipt(
            method="GET",
            request_url=url,
            request_body=None,
            response_body=visible,
            body_truncated=True,
            body_spilled_complete=True,
            wire_body_complete=True,
            pagination_scan_body=full_body,
            response_bytes_read=len(full_body.encode("utf-8")),
            response_byte_limit=400_000,
            response_chars_read=len(full_body),
            response_chars_returned=len(visible),
            response_char_limit=100,
            response_char_hard_limit=100_000,
            request_number=1,
            request_run_hop_limit=16,
            request_elapsed_ms=5,
        )
        responses = [
            _tool_call_response(
                "call-inventory-spill",
                tool_name="skill_http_get",
                arguments={"url": url, "max_chars": 100},
            ),
            _tool_call_response(
                "call-inventory-readback",
                tool_name="read_tool_result",
                arguments={"handle": handle, "pattern": "records"},
            ),
            _stop_response(
                "# Inventory findings\n"
                "The bounded readback supplied the retained evidence.\n"
                'RESULT_FIELDS_JSON: {"evidence":'
            ),
            _result_fields_submit_response("evidence"),
        ]
        dispatch_results = [
            json.dumps({
                "status": "success",
                "request_sent": True,
                "request_number": 1,
                "url": url,
                "body_result_handle": handle,
                "body": visible,
                "body_chars": len(visible),
                "body_truncated": True,
                "body_spilled_complete": True,
                "retrieval": receipt,
            }),
            json.dumps({
                "status": "success",
                "handle": handle,
                "content": '"records":[{"note":"' + ("x" * 20),
                "start_offset": 1,
                "end_offset": 42,
                "total_chars": len(full_body),
                "has_more_before": True,
                "has_more_after": True,
            }),
        ]
        schemas = [*self.http_schema, {
            "type": "function",
            "function": READ_TOOL_RESULT_SCHEMA,
        }]

        def schema_resolver(names):
            selected = set(names)
            return [
                schema
                for schema in schemas
                if schema["function"]["name"] in selected
            ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result="",
            dispatch_results=dispatch_results,
            tools=["skill_http_get"],
            schemas=schemas,
            schema_resolver=schema_resolver,
            required_result_fields=["evidence"],
            required_capability_tools=["skill_http_get"],
            allowed_skill_http_prefixes=[(
                "inventory-api", "https://api.inventory.test/v1/records"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(2, dispatch_mock.await_count)
        self.assertEqual(4, len(request_bodies))
        self.assertIn("read_tool_result", {
            item["function"]["name"]
            for item in request_bodies[1]["tools"]
        })
        self.assertNotIn("tools", request_bodies[2])
        self.assertEqual(
            "submit_result_fields",
            request_bodies[3]["tools"][0]["function"]["name"],
        )
        requested = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
        )
        self.assertEqual("ordinary_stop", requested["payload"]["origin"])
        self.assertFalse(any(
            event.get("event_type")
            == "debug.delegate.result_footer_repair.unavailable"
            for event in events
        ))
        completed = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.completed"
        )
        self.assertTrue(completed["payload"]["footer_valid"])
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_repage_action_is_injected_into_exact_http_followup_turn(self):
        root = "https://api.vendor.test/search?q=evidence&pageSize=50"
        full = json.dumps({
            "items": [{"text": "x" * 300}],
            "nextPageToken": None,
        })
        visible = full[:100]
        first_receipt = build_http_retrieval_receipt(
            method="GET",
            request_url=root,
            request_body=None,
            response_body=visible,
            pagination_scan_body=full,
            body_truncated=True,
            wire_body_complete=True,
            response_bytes_read=len(full.encode("utf-8")),
            response_byte_limit=400_000,
            response_chars_read=len(full),
            response_chars_returned=len(visible),
            response_char_limit=100,
            response_char_hard_limit=100,
            request_timeout=20,
            request_number=1,
            request_run_hop_limit=16,
            request_elapsed_ms=5,
        )
        action = first_receipt["continuation_action"]
        self.assertEqual("restart_with_smaller_page", action["kind"])
        smaller_url = action["args"]["url"]
        second_body = '{"items":[1],"nextPageToken":null}'
        dispatch_results = [
            json.dumps({
                "status": "success",
                "request_sent": True,
                "request_number": 1,
                "url": root,
                "body": visible,
                "body_chars": len(visible),
                "body_truncated": True,
                "retrieval": first_receipt,
            }),
            _http_success_result(
                smaller_url,
                second_body,
                request_number=2,
                max_chars=100,
            ),
        ]
        responses = [
            _tool_call_response(
                "call-http-oversized",
                tool_name="skill_http_get",
                arguments={"url": root, "max_chars": 100},
            ),
            _tool_call_response(
                "call-http-repage",
                tool_name="skill_http_get",
                arguments=action["args"],
            ),
            _stop_response(
                "Replacement page chain closed.\n"
                + _result_fields_footer("evidence")
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result="",
            dispatch_results=dispatch_results,
            tools=["skill_http_get"],
            schemas=self.http_schema,
            required_result_fields=["evidence"],
            required_capability_tools=["skill_http_get"],
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/search"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(2, dispatch_mock.await_count)
        followup = request_bodies[1]
        self.assertEqual("required", followup.get("tool_choice"))
        self.assertEqual(
            {"enable_thinking": False},
            followup.get("chat_template_kwargs"),
        )
        self.assertEqual(
            ["skill_http_get"],
            [item["function"]["name"] for item in followup["tools"]],
        )
        followup_history = json.dumps(
            followup["messages"], ensure_ascii=False
        )
        self.assertIn("CONTINUATION_ACTION_JSON", followup_history)
        self.assertIn(smaller_url, followup_history)
        gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegated_http_retrieval_completeness"
        ]
        self.assertEqual(1, len(gates))
        self.assertTrue(gates[0]["payload"]["machine_action_injected"])
        self.assertEqual(
            "restart_with_smaller_page",
            gates[0]["payload"]["continuation_action"]["kind"],
        )
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_required_http_followup_ignored_length_retries_exact_call_once(self):
        root = "https://api.vendor.test/search?q=evidence&pageSize=50"
        full = json.dumps({
            "items": [{"text": "x" * 300}],
            "nextPageToken": None,
        })
        visible = full[:100]
        first_receipt = build_http_retrieval_receipt(
            method="GET",
            request_url=root,
            request_body=None,
            response_body=visible,
            pagination_scan_body=full,
            body_truncated=True,
            wire_body_complete=True,
            response_bytes_read=len(full.encode("utf-8")),
            response_byte_limit=400_000,
            response_chars_read=len(full),
            response_chars_returned=len(visible),
            response_char_limit=100,
            response_char_hard_limit=100,
            request_timeout=20,
            request_number=1,
            request_run_hop_limit=16,
            request_elapsed_ms=5,
        )
        action = first_receipt["continuation_action"]
        smaller_url = action["args"]["url"]
        ignored = (
            "This truncated prose must be discarded because the exact "
            "machine-owned HTTP continuation was not dispatched."
        )
        responses = [
            _tool_call_response(
                "call-http-oversized-before-ignore",
                tool_name="skill_http_get",
                arguments={"url": root, "max_chars": 100},
            ),
            _visible_length_response(ignored),
            _tool_call_response(
                "call-http-repage-after-ignore",
                tool_name="skill_http_get",
                arguments=action["args"],
            ),
            _stop_response(
                "Replacement page chain closed.\n"
                + _result_fields_footer("evidence")
            ),
        ]
        dispatch_results = [
            json.dumps({
                "status": "success",
                "request_sent": True,
                "request_number": 1,
                "url": root,
                "body": visible,
                "body_chars": len(visible),
                "body_truncated": True,
                "retrieval": first_receipt,
            }),
            _http_success_result(
                smaller_url,
                '{"items":[1],"nextPageToken":null}',
                request_number=2,
                max_chars=100,
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result="",
            dispatch_results=dispatch_results,
            tools=["skill_http_get"],
            schemas=self.http_schema,
            required_result_fields=["evidence"],
            required_capability_tools=["skill_http_get"],
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/search"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(2, dispatch_mock.await_count)
        self.assertEqual(4, len(request_bodies))
        for body in request_bodies[1:3]:
            self.assertEqual("required", body.get("tool_choice"))
            self.assertEqual(2_048, body.get("max_tokens"))
            self.assertEqual(
                ["skill_http_get"],
                [item["function"]["name"] for item in body["tools"]],
            )
        recovery_messages = request_bodies[2]["messages"]
        self.assertEqual(2, len(recovery_messages))
        self.assertEqual(["system", "user"], [
            message["role"] for message in recovery_messages
        ])
        recovery_snapshot = str(recovery_messages[1]["content"])
        self.assertIn("required_http_retrieval_action", recovery_snapshot)
        self.assertIn(smaller_url, recovery_snapshot)
        self.assertNotIn(ignored, recovery_snapshot)
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertNotIn(ignored, emitted)
        requested = [
            event["payload"]
            for event in events
            if event.get("event_type")
            == "debug.delegate.required_capability_noncall_recovery.requested"
        ]
        self.assertEqual(1, len(requested))
        self.assertEqual(
            "required_http_retrieval_continuation",
            requested[0]["required_call_kind"],
        )
        self.assertEqual("length", requested[0]["provider_finish_reason"])
        self.assertFalse(any(
            event.get("event_type") == "run.failed"
            for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_next_page_token_chain_must_reach_end_before_synthesis(self):
        root = "https://api.vendor.test/search?q=evidence"
        page2 = root + "&pageToken=A"
        page3 = root + "&pageToken=B"
        responses = [
            _tool_call_response(
                "call-page-1",
                tool_name="skill_http_get",
                arguments={"url": root},
            ),
            _tool_call_response(
                "call-page-2",
                tool_name="skill_http_get",
                arguments={"url": page2},
            ),
            _tool_call_response(
                "call-page-3",
                tool_name="skill_http_get",
                arguments={"url": page3},
            ),
            _stop_response(
                "All cursor pages closed.\n"
                + _result_fields_footer("evidence")
            ),
        ]
        dispatch_results = [
            _http_success_result(
                root,
                '{"items":[1],"nextPageToken":"A"}',
                request_number=1,
            ),
            _http_success_result(
                page2,
                '{"items":[2],"nextPageToken":"B"}',
                request_number=2,
            ),
            _http_success_result(
                page3,
                '{"items":[3],"nextPageToken":null}',
                request_number=3,
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result="",
            dispatch_results=dispatch_results,
            tools=["skill_http_get"],
            schemas=self.http_schema,
            required_result_fields=["evidence"],
            retrieval_completeness_policy="exhaustive",
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/search"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(3, dispatch_mock.await_count)
        self.assertEqual(
            [root, page2, page3],
            [call.args[1]["url"] for call in dispatch_mock.await_args_list],
        )
        receipt_states = [
            event["payload"]["open_chain_count"]
            for event in events
            if event.get("event_type")
            == "debug.http_retrieval.receipt"
        ]
        self.assertEqual([1, 1, 0], receipt_states)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_bounded_clean_cursor_stop_queues_one_partial_synthesis(self):
        root = "https://api.museum.test/catalog?q=bronze"
        page2 = root + "&pageToken=A"
        responses = [
            _tool_call_response(
                "call-museum-page-1",
                tool_name="skill_http_get",
                arguments={"url": root},
            ),
            _stop_response(
                "Observed the first catalog page; another page is available.\n"
                + _result_fields_footer("catalog_evidence")
            ),
            _stop_response(
                "One bounded catalog page was observed; another page remains "
                "available and no exhaustive coverage is claimed.\n"
                + _result_fields_footer("catalog_evidence")
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result=_http_success_result(
                root,
                '{"objects":[{"id":1}],"nextPageToken":"A"}',
                request_number=1,
            ),
            tools=["skill_http_get"],
            schemas=self.http_schema,
            required_result_fields=["catalog_evidence"],
            required_capability_tools=["skill_http_get"],
            allowed_skill_http_prefixes=[(
                "museum-catalog", "https://api.museum.test/catalog"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(1, dispatch_mock.await_count)
        self.assertEqual(3, len(request_bodies))
        optional_request = request_bodies[1]
        self.assertNotIn("tool_choice", optional_request)
        self.assertEqual(8192, optional_request["max_tokens"])
        self.assertEqual(
            ["skill_http_get"],
            [item["function"]["name"] for item in optional_request["tools"]],
        )
        self.assertNotIn("tools", request_bodies[2])
        completed = [
            event["payload"]
            for event in events
            if event.get("event_type") == "run.completed"
            and isinstance(
                event.get("payload", {}).get("unresolved_retrieval"),
                dict,
            )
        ]
        self.assertEqual(1, len(completed))
        self.assertNotEqual(
            "degraded",
            completed[0].get("completion_quality"),
        )
        gap = completed[0]["unresolved_retrieval"]
        self.assertEqual("advisory", gap["quality_impact"])
        self.assertEqual("bounded", gap["retrieval_completeness_policy"])
        self.assertEqual("partial", gap["coverage_status"])
        self.assertEqual(
            "bounded_acquisition_model_stop_with_open_pagination",
            gap["closure_reason"],
        )
        self.assertIsNone(gap["terminal_failure"])
        self.assertEqual(1, gap["frontier_receipt"]["next_cursor_count"])
        family = gap["frontier_receipt"]["families"][0]
        self.assertEqual(1, family["pages_observed"])
        self.assertEqual(1, family["items_observed"])
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})
        self.assertNotIn(page2, [
            call.args[1].get("url")
            for call in dispatch_mock.await_args_list
        ])

    async def test_bounded_cursor_closes_at_two_turn_synthesis_reserve(self):
        root = "https://api.museum.test/catalog?q=textile"
        responses = [
            _tool_call_response(
                "call-museum-reserve-page-1",
                tool_name="skill_http_get",
                arguments={"url": root},
            ),
            _stop_response(
                "The bounded catalog acquisition ended at the synthesis "
                "reserve without claiming exhaustive coverage.\n"
                + _result_fields_footer("catalog_evidence")
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=_http_success_result(
                root,
                '{"items":[1],"nextPageToken":"NEXT"}',
                request_number=1,
            ),
            tools=["skill_http_get"],
            schemas=self.http_schema,
            required_result_fields=["catalog_evidence"],
            required_capability_tools=["skill_http_get"],
            allowed_skill_http_prefixes=[(
                "museum-catalog", "https://api.museum.test/catalog"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(1, dispatch_mock.await_count)
        self.assertEqual(2, len(request_bodies))
        self.assertNotIn("tools", request_bodies[1])
        completed = [
            event["payload"]
            for event in events
            if event.get("event_type") == "run.completed"
            and isinstance(
                event.get("payload", {}).get("unresolved_retrieval"),
                dict,
            )
        ]
        self.assertEqual(1, len(completed))
        self.assertNotEqual(
            "degraded",
            completed[0].get("completion_quality"),
        )
        gap = completed[0]["unresolved_retrieval"]
        self.assertEqual("advisory", gap["quality_impact"])
        self.assertEqual(
            "bounded_acquisition_synthesis_reserve",
            gap["closure_reason"],
        )
        self.assertEqual(
            "synthesis_turn_reserve",
            gap["budget_receipt"]["closure_trigger"],
        )
        self.assertEqual(2, gap["budget_receipt"]["iterations_remaining"])
        self.assertIsNone(gap["terminal_failure"])
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_retrieval_closure_never_reopens_during_output_repair(self):
        url = "https://api.vendor.test/search?q=closure"
        raw_protocol = (
            "<tool_call><name>skill_http_get</name>"
            "<arguments>{}</arguments></tool_call>"
        )
        responses = [
            _tool_call_response(
                "call-http-before-closure",
                tool_name="skill_http_get",
                arguments={"url": url, "max_chars": 10},
            ),
            _stop_response(
                "Bounded evidence draft with invalid raw protocol.\n"
                + raw_protocol
            ),
            _tool_call_response(
                "call-submit-evidence-fields",
                tool_name="submit_result_fields",
                arguments={
                    "evidence": {
                        "status": "degraded",
                        "reason": "authorized bounded source was truncated",
                        "provenance": "authoritative HTTP tool receipt",
                    },
                },
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=_http_success_result(
                url,
                '{"items":[1,2,3]}',
                request_number=1,
                body_truncated=True,
                max_chars=10,
            ),
            tools=["skill_http_get"],
            schemas=self.http_schema,
            required_result_fields=["evidence"],
            required_capability_tools=["skill_http_get"],
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/search"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(1, dispatch_mock.await_count)
        self.assertEqual(3, len(request_bodies))
        for request in request_bodies[1:]:
            exposed = [
                item["function"]["name"]
                for item in request.get("tools") or []
            ]
            self.assertNotIn("skill_http_get", exposed)
        self.assertEqual(
            ["submit_result_fields"],
            [
                item["function"]["name"]
                for item in request_bodies[2].get("tools") or []
            ],
        )
        self.assertEqual("required", request_bodies[2].get("tool_choice"))
        self.assertFalse(any(
            event.get("event_type") == "run.failed"
            for event in events
        ))
        iteration_starts = [
            event["payload"]
            for event in events
            if event.get("event_type") == "debug.iteration.started"
        ]
        self.assertTrue(iteration_starts)
        self.assertTrue(all(
            item.get("delegate_http_retrieval_phase") == "closing"
            for item in iteration_starts[1:]
        ))
        self.assertFalse(any(
            item.get("delegate_http_retrieval_followup") is True
            and (
                item.get("delegate_output_contract_repair") is True
                or item.get("delegate_visible_length_recovery") is True
            )
            for item in iteration_starts
        ))
        self.assertTrue(any(
            event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
            for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_non_paginated_http_response_keeps_normal_completion(self):
        url = "https://api.vendor.test/search?q=one"
        responses = [
            _tool_call_response(
                "call-http-complete",
                tool_name="skill_http_get",
                arguments={"url": url},
            ),
            _stop_response(
                "One complete response.\n"
                + _result_fields_footer("evidence")
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=3,
            dispatch_result=_http_success_result(
                url,
                '{"items":[1]}',
                request_number=1,
            ),
            tools=["skill_http_get"],
            schemas=self.http_schema,
            required_result_fields=["evidence"],
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/search"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(1, dispatch_mock.await_count)
        self.assertEqual(2, len(request_bodies))
        synthesis_history = "\n".join(
            str(message.get("content") or "")
            for message in request_bodies[1]["messages"]
        )
        self.assertIn("EVIDENCE_COUNT_LEDGER_JSON", synthesis_history)
        self.assertIn('"items_observed":1', synthesis_history)
        self.assertIn(
            "items_observed is only the sum", synthesis_history
        )
        self.assertIn("does not prove unique records", synthesis_history)
        ledgers = [
            event["payload"]
            for event in events
            if event.get("event_type")
            == "debug.http_retrieval.evidence_ledger"
        ]
        self.assertEqual(1, len(ledgers))
        self.assertEqual(1, ledgers[0]["families"][0]["items_observed"])
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegated_http_retrieval_completeness"
            for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_sequential_closed_families_replace_prior_ledger_prompt(self):
        first_url = "https://api.vendor.test/search?q=first"
        second_url = "https://api.vendor.test/search?q=second"
        responses = [
            _tool_call_response(
                "call-http-first",
                tool_name="skill_http_get",
                arguments={"url": first_url},
            ),
            _tool_call_response(
                "call-http-second",
                tool_name="skill_http_get",
                arguments={"url": second_url},
            ),
            _stop_response(
                "Two bounded response families observed.\n"
                + _result_fields_footer("evidence")
            ),
        ]
        dispatch_results = [
            _http_success_result(
                first_url, '{"items":[1]}', request_number=1
            ),
            _http_success_result(
                second_url, '{"items":[2,3]}', request_number=2
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result="",
            dispatch_results=dispatch_results,
            tools=["skill_http_get"],
            schemas=self.http_schema,
            required_result_fields=["evidence"],
            required_capability_tools=["skill_http_get"],
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/search"
            )],
        )

        self.assertFalse(responses)
        self.assertEqual(2, dispatch_mock.await_count)
        self.assertEqual(3, len(request_bodies))
        first_synthesis_history = "\n".join(
            str(message.get("content") or "")
            for message in request_bodies[1]["messages"]
        )
        latest_synthesis_history = "\n".join(
            str(message.get("content") or "")
            for message in request_bodies[2]["messages"]
        )
        marker = "[Harness machine-owned HTTP evidence count ledger]"
        self.assertEqual(1, first_synthesis_history.count(marker))
        self.assertEqual(1, latest_synthesis_history.count(marker))
        self.assertIn('"family_count":2', latest_synthesis_history)
        self.assertIn('"items_observed":1', latest_synthesis_history)
        self.assertIn('"items_observed":2', latest_synthesis_history)
        ledger_events = [
            event for event in events
            if event.get("event_type")
            == "debug.http_retrieval.evidence_ledger"
        ]
        self.assertEqual(2, len(ledger_events))

    async def test_primary_chat_does_not_enable_delegated_retrieval_gate(self):
        url = "https://api.vendor.test/search?q=one"
        responses = [
            _tool_call_response(
                "call-primary-http",
                tool_name="skill_http_get",
                arguments={"url": url, "max_chars": 10},
            ),
            _stop_response("The bounded primary response was truncated."),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=2,
            dispatch_result=_http_success_result(
                url,
                '{"items":[1,2,3]}',
                request_number=1,
                body_truncated=True,
                max_chars=10,
            ),
            tools=["skill_http_get"],
            schemas=self.http_schema,
            delegated=False,
            required_result_fields=["evidence"],
            allowed_skill_http_prefixes=[(
                "evidence-api", "https://api.vendor.test/search"
            )],
            user_content=(
                "Use skill_http_get once on the exact authorized URL, then "
                "report its bounded response."
            ),
        )

        self.assertFalse(responses)
        self.assertEqual(1, dispatch_mock.await_count)
        self.assertEqual(2, len(request_bodies))
        self.assertFalse(any(
            "http_retrieval" in str(event.get("event_type") or "")
            for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_retrieval_hard_limit_completes_with_machine_degraded_receipt(self):
        url = "https://api.vendor.test/search?q=evidence"
        responses = [
            _tool_call_response(
                "call-http-page-limit",
                tool_name="skill_http_get",
                arguments={"url": url},
            ),
            _stop_response(
                "Status: WARN/degraded because retrieval is unresolved.\n"
                + _result_fields_footer("evidence", degraded=True)
            ),
        ]

        with patch(
            "agent_loop.settings.delegated_retrieval_max_pages_per_chain",
            1,
        ):
            request_bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=3,
                dispatch_result=_http_success_result(
                    url,
                    '{"items":[1],"nextPageToken":"A"}',
                    request_number=1,
                ),
                tools=["skill_http_get"],
                schemas=self.http_schema,
                required_result_fields=["evidence"],
                allowed_skill_http_prefixes=[(
                    "evidence-api", "https://api.vendor.test/search"
                )],
            )

        self.assertFalse(responses)
        self.assertEqual(1, dispatch_mock.await_count)
        self.assertEqual(2, len(request_bodies))
        self.assertNotIn("tools", request_bodies[1])
        authoritative = [
            event
            for event in events
            if event.get("event_type") == "run.completed"
            and event.get("payload", {}).get("completion_quality")
            == "degraded"
        ]
        self.assertEqual(1, len(authoritative))
        gap = authoritative[0]["payload"]["unresolved_retrieval"]
        self.assertEqual("unresolved", gap["status"])
        self.assertEqual("degraded", gap["quality_impact"])
        self.assertEqual("pagination_page_limit", gap["terminal_reason"])
        self.assertGreaterEqual(gap["open_frontier_count"], 1)
        self.assertFalse(any(
            event.get("event_type") == "run.failed"
            for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_terminal_retrieval_waits_for_pending_exact_sibling(self):
        """A failed evidence family must not close an independent DAG node."""

        url = "https://api.inventory.test/v1/catalog?q=archival"
        prefix = "https://api.inventory.test/v1/catalog"
        skill_temp = tempfile.TemporaryDirectory()
        self.addCleanup(skill_temp.cleanup)
        skill_root = Path(skill_temp.name)
        skill_md = skill_root / "SKILL.md"
        skill_md.write_text(
            "# Inventory Audit\n\nUse the exact declared catalog endpoint.\n",
            encoding="utf-8",
        )
        skill_md_sha256 = hashlib.sha256(skill_md.read_bytes()).hexdigest()
        package_sha256 = compute_skill_package_digest(skill_root)
        resolve_patch = patch(
            "skills.scanner.resolve_skill_path",
            return_value=skill_md,
        )
        resolve_patch.start()
        self.addCleanup(resolve_patch.stop)
        plan = {
            "schema_version": 1,
            "worker_id": "inventory-auditor",
            "owner_skill": "inventory-audit",
            "checks": [{
                "id": "KG-INVENTORY",
                "question": "Are both independent catalog receipts required?",
                "branches": [{
                    "outcome": "yes",
                    "action": "Retrieve one receipt for each independent node.",
                    "group_ids": ["catalog-a", "catalog-b"],
                }],
                "legacy_ambiguous": False,
            }],
            "groups": [
                {
                    "id": group_id,
                    "check_id": "KG-INVENTORY",
                    "outcome": "yes",
                    "mode": "one_of",
                    "candidate_ids": ["candidate-http"],
                    "selectors": ["skill_http_get"],
                    "unresolved_selectors": [],
                }
                for group_id in ("catalog-a", "catalog-b")
            ],
            "candidates": [{
                "candidate_id": "candidate-http",
                "kind": "skill_http_prefix",
                "tool_name": "skill_http_get",
                "tool_names": ["skill_http_get"],
                "skill_name": "inventory-audit",
                "skill_md_sha256": skill_md_sha256,
                "package_sha256": package_sha256,
                "url_prefix": prefix,
                "http_method": "GET",
            }],
        }
        digest = canonical_json_sha256(plan)
        accepted = validate_knowledge_gate_decisions(
            plan,
            expected_sha256=digest,
            supplied_sha256=digest,
            decisions=[{
                "check_id": "KG-INVENTORY",
                "outcome": "yes",
                "reason": "Both independent inventory receipts are required.",
            }],
        )
        authority, authority_error = _exact_knowledge_gate_candidate_grants(
            plan,
            context=ToolContext(
                user_id="u-delegate-convergence",
                session_id="s-delegate-convergence",
                enabled_tools=(
                    KNOWLEDGE_GATE_DECISION_TOOL_NAME,
                    "skill_http_get",
                ),
                skill_execution_resource_boundary=True,
                allowed_skill_resources=((
                    "inventory-audit", "SKILL.md"
                ),),
                allowed_skill_package_digests=((
                    "inventory-audit", package_sha256
                ),),
                allowed_skill_http_prefixes=((
                    "inventory-audit", prefix
                ),),
            ),
        )
        self.assertIsNone(authority_error)

        decision_arguments = {
            "plan_sha256": digest,
            "decisions": [{
                "check_id": "KG-INVENTORY",
                "outcome": "yes",
                "reason": "Both independent inventory receipts are required.",
            }],
        }
        responses = [
            _tool_call_response(
                "call-inventory-decision",
                tool_name=KNOWLEDGE_GATE_DECISION_TOOL_NAME,
                arguments=decision_arguments,
            ),
            _tool_call_response(
                "call-inventory-a",
                tool_name="skill_http_get",
                arguments={"url": url},
            ),
            _tool_call_response(
                "call-inventory-b",
                tool_name="skill_http_get",
                arguments={"url": url},
            ),
            _stop_response(
                "Status: WARN/degraded; both exact nodes ran, while the "
                "pagination gap remains explicit.\n"
                + _result_fields_footer("catalog_evidence", degraded=True)
            ),
        ]
        dispatch_results = [
            json.dumps(accepted),
            _http_success_result(
                url,
                '{"items":[1],"nextPageToken":"NEXT"}',
                request_number=1,
            ),
            _http_success_result(
                url,
                '{"items":[2]}',
                request_number=2,
            ),
        ]

        with patch(
            "agent_loop.settings.delegated_retrieval_max_pages_per_chain",
            1,
        ):
            request_bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=6,
                dispatch_result="",
                dispatch_results=dispatch_results,
                tools=["skill_http_get"],
                schemas=self.http_schema,
                required_result_fields=["catalog_evidence"],
                allowed_skill_http_prefixes=[(
                    "inventory-audit", prefix
                )],
                allowed_skill_resources=[(
                    "inventory-audit", "SKILL.md"
                )],
                allowed_skill_package_digests=[(
                    "inventory-audit", package_sha256
                )],
                knowledge_gate_plan=plan,
                knowledge_gate_plan_sha256=digest,
                knowledge_gate_candidate_authority=authority,
            )

        self.assertFalse(responses)
        self.assertEqual(3, dispatch_mock.await_count)
        self.assertEqual(
            [
                KNOWLEDGE_GATE_DECISION_TOOL_NAME,
                "skill_http_get",
                "skill_http_get",
            ],
            [call.args[0] for call in dispatch_mock.await_args_list],
        )
        self.assertEqual(4, len(request_bodies))
        self.assertEqual(
            ["skill_http_get"],
            [
                item["function"]["name"]
                for item in request_bodies[2]["tools"]
            ],
        )
        self.assertNotIn("tools", request_bodies[3])
        self.assertEqual(1, len([
            event for event in events
            if event.get("event_type")
            == "debug.http_retrieval.degraded_synthesis_deferred"
        ]))
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def _run(
        self,
        responses,
        *,
        max_iterations,
        dispatch_result,
        tools=None,
        schemas=None,
        delegated=True,
        required_result_fields=None,
        required_result_schema=None,
        max_tokens=8192,
        retrieval_completeness_policy=None,
        required_capability_tools=None,
        allowed_skill_scripts=None,
        allowed_skill_resources=None,
        allowed_skill_package_digests=None,
        allowed_skill_http_prefixes=None,
        dispatch_results=None,
        turn_boundary_events=None,
        user_content=None,
        schema_resolver=None,
        knowledge_gate_plan=None,
        knowledge_gate_plan_sha256=None,
        knowledge_gate_candidate_authority=None,
    ):
        request_bodies = []
        tools = list(tools or ["web_search"])
        schemas = list(schemas or self.web_schema)

        class FakeResponse:
            status_code = 200

            def __init__(self, lines):
                self._lines = lines

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aiter_lines(self):
                for line in self._lines:
                    yield line
                    if isinstance(line, str) and line.startswith("data"):
                        yield ""

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                request_bodies.append(kwargs.get("json"))
                return FakeResponse(responses.pop(0))

        dispatch_mock = (
            AsyncMock(side_effect=list(dispatch_results))
            if dispatch_results is not None
            else AsyncMock(return_value=dispatch_result)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", dispatch_mock),
                patch(
                    "agent_loop.get_schemas",
                    side_effect=schema_resolver,
                ) if schema_resolver is not None else patch(
                    "agent_loop.get_schemas", return_value=schemas
                ),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("agent_loop.settings.agent_debug_trace", True),
                patch("agent_loop._append_workspace_debug_event") as debug_append,
                patch("skills.scanner.find_all_skills", return_value=[]),
                # These convergence fixtures inject synthetic schemas and
                # mocked dispatch results for nonexistent scripts. The real
                # AST-backed runner preflight has its own integration tests;
                # disable it here so this suite continues to isolate the
                # agent-loop denial/quarantine state machine.
                patch.object(
                    native_tool_registry.get_entry("run_skill_python"),
                    "args_preflight_fn",
                    None,
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-delegate-convergence",
                        [{
                            "role": "user",
                            "content": (
                                user_content
                                or "Execute this bounded evidence task and return a "
                                "typed result with provenance and gaps."
                            ),
                        }],
                        tools,
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-delegate-convergence",
                        session_id="s-delegate-convergence",
                        source="delegate" if delegated else "chat",
                        agent_kind="delegate" if delegated else "primary",
                        max_iterations=max_iterations,
                        max_tokens=max_tokens,
                        required_result_fields=required_result_fields,
                        required_result_schema=required_result_schema,
                        retrieval_completeness_policy=(
                            retrieval_completeness_policy
                        ),
                        required_capability_tools=required_capability_tools,
                        allowed_skill_resources=allowed_skill_resources,
                        allowed_skill_scripts=allowed_skill_scripts,
                        allowed_skill_package_digests=(
                            allowed_skill_package_digests
                        ),
                        allowed_skill_http_prefixes=(
                            allowed_skill_http_prefixes
                        ),
                        turn_boundary_sink=(
                            turn_boundary_events.append
                            if turn_boundary_events is not None
                            else None
                        ),
                        knowledge_gate_plan=knowledge_gate_plan,
                        knowledge_gate_plan_sha256=(
                            knowledge_gate_plan_sha256
                        ),
                        knowledge_gate_candidate_authority=(
                            knowledge_gate_candidate_authority
                        ),
                    )
                ]
        persisted_debug_events = [
            call.args[2] for call in debug_append.call_args_list
        ]
        return request_bodies, dispatch_mock, events, persisted_debug_events

    async def test_required_capability_ignored_stop_recovers_with_valid_call(self):
        ignored_body = (
            "This prose must remain transactional because no required "
            "capability was dispatched."
        )
        final_body = (
            "status: PASS\n"
            "Evidence: the required capability crossed the handler boundary.\n"
            "Gap: none"
        )
        responses = [
            _stop_response(ignored_body),
            _tool_call_response(
                "call-required-after-ignored-stop",
                tool_name="web_search",
                arguments={"query": "bounded evidence"},
            ),
            _stop_response(final_body),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({
                "status": "success",
                "results": [{"title": "bounded evidence"}],
            }),
            required_capability_tools=["web_search"],
            user_content=(
                "GENERIC_TASK_START\n"
                + ("large prior context " * 2_000)
                + "\nGENERIC_TASK_END"
            ),
        )

        self.assertFalse(responses)
        self.assertEqual(3, len(request_bodies))
        self.assertEqual(1, dispatch_mock.await_count)
        for body in request_bodies[:2]:
            self.assertEqual("required", body.get("tool_choice"))
            self.assertEqual(
                {"enable_thinking": False},
                body.get("chat_template_kwargs"),
            )
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertNotIn(ignored_body, emitted)
        self.assertIn(final_body, emitted)
        self.assertFalse(any(
            event.get("event_type") == "run.failed"
            for event in events
        ))
        self.assertEqual(
            {"type": "done", "finish_reason": "stop"},
            events[-1],
        )

    async def test_required_capability_two_ignored_stops_fail_closed(self):
        first_ignored_body = "First ignored required-call response must not leak."
        second_ignored_body = "Second ignored required-call response must not leak."
        responses = [
            _stop_response(first_ignored_body),
            _stop_response(second_ignored_body),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({"status": "must-not-dispatch"}),
            required_capability_tools=["web_search"],
        )

        self.assertEqual(2, len(request_bodies))
        self.assertEqual(1, len(responses))
        self.assertEqual(0, dispatch_mock.await_count)
        for body in request_bodies:
            self.assertEqual("required", body.get("tool_choice"))
            self.assertEqual(
                {"enable_thinking": False},
                body.get("chat_template_kwargs"),
            )
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertNotIn(first_ignored_body, emitted)
        self.assertNotIn(second_ignored_body, emitted)
        failed = [
            event
            for event in events
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(1, len(failed))
        self.assertEqual(
            "required_capability_not_attempted",
            failed[0]["payload"]["finish_reason"],
        )
        self.assertEqual("error", events[-1]["type"])

    async def test_required_capability_ignored_length_recovers_with_valid_call(self):
        ignored_body = (
            "This length-limited prose is not a required capability receipt."
        )
        final_body = (
            "status: PASS\n"
            "Evidence: the required capability crossed the handler boundary.\n"
            "Gap: none"
        )
        responses = [
            _visible_length_response(ignored_body),
            _tool_call_response(
                "call-required-after-ignored-length",
                tool_name="web_search",
                arguments={"query": "bounded evidence"},
            ),
            _stop_response(final_body),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({
                "status": "success",
                "results": [{"title": "bounded evidence"}],
            }),
            required_capability_tools=["web_search"],
        )

        self.assertFalse(responses)
        self.assertEqual(3, len(request_bodies))
        self.assertEqual(1, dispatch_mock.await_count)
        for body in request_bodies[:2]:
            self.assertEqual("required", body.get("tool_choice"))
            self.assertEqual(False, body.get("parallel_tool_calls"))
            self.assertEqual(2_048, body.get("max_tokens"))
            self.assertEqual(1, len(body.get("tools") or []))
            self.assertEqual(
                {"enable_thinking": False},
                body.get("chat_template_kwargs"),
            )
        correction_messages = request_bodies[1]["messages"]
        self.assertEqual(2, len(correction_messages))
        self.assertEqual(["system", "user"], [
            message["role"] for message in correction_messages
        ])
        self.assertLess(
            sum(len(str(message.get("content") or "")) for message in correction_messages),
            20_000,
        )
        self.assertTrue(any(
            message.get("role") == "user"
            and "Harness machine-owned mandatory phase snapshot" in str(
                message.get("content") or ""
            )
            for message in correction_messages
        ))
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertNotIn(ignored_body, emitted)
        self.assertIn(final_body, emitted)
        requested = [
            event for event in events
            if event.get("event_type")
            == "debug.delegate.required_capability_noncall_recovery.requested"
        ]
        self.assertEqual(1, len(requested))
        self.assertEqual(
            "length",
            requested[0]["payload"]["provider_finish_reason"],
        )
        self.assertEqual(
            0,
            requested[0]["payload"][
                "actual_required_capability_dispatch_count"
            ],
        )
        self.assertFalse(any(
            event.get("event_type") == "run.failed"
            for event in events
        ))
        self.assertEqual(
            {"type": "done", "finish_reason": "stop"},
            events[-1],
        )

    async def test_required_capability_two_ignored_lengths_fail_closed(self):
        first_ignored_body = "First truncated prose must not leak."
        second_ignored_body = "Second truncated prose must not leak."
        responses = [
            _visible_length_response(first_ignored_body),
            _visible_length_response(second_ignored_body),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({"status": "must-not-dispatch"}),
            required_capability_tools=["web_search"],
        )

        self.assertEqual(2, len(request_bodies))
        self.assertEqual(1, len(responses))
        self.assertEqual(0, dispatch_mock.await_count)
        for body in request_bodies:
            self.assertEqual("required", body.get("tool_choice"))
            self.assertEqual(False, body.get("parallel_tool_calls"))
            self.assertEqual(2_048, body.get("max_tokens"))
            self.assertEqual(1, len(body.get("tools") or []))
            self.assertEqual(
                {"enable_thinking": False},
                body.get("chat_template_kwargs"),
            )
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertNotIn(first_ignored_body, emitted)
        self.assertNotIn(second_ignored_body, emitted)
        failed = [
            event for event in events
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(1, len(failed))
        self.assertEqual(
            "required_capability_not_attempted",
            failed[0]["payload"]["finish_reason"],
        )
        self.assertEqual(
            "length",
            failed[0]["payload"]["provider_finish_reason"],
        )
        self.assertEqual(
            0,
            failed[0]["payload"][
                "actual_required_capability_dispatch_count"
            ],
        )
        self.assertEqual("error", events[-1]["type"])

    async def test_required_capability_stop_and_length_share_one_recovery_budget(self):
        cases = (
            ("stop", "length"),
            ("length", "stop"),
        )
        for first_finish, second_finish in cases:
            with self.subTest(
                first_finish=first_finish,
                second_finish=second_finish,
            ):
                first_body = f"ignored first {first_finish} output"
                second_body = f"ignored second {second_finish} output"
                first_response = (
                    _stop_response(first_body)
                    if first_finish == "stop"
                    else _visible_length_response(first_body)
                )
                second_response = (
                    _stop_response(second_body)
                    if second_finish == "stop"
                    else _visible_length_response(second_body)
                )
                responses = [
                    first_response,
                    second_response,
                    _stop_response("must not be requested"),
                ]

                request_bodies, dispatch_mock, events, _persisted = (
                    await self._run(
                        responses,
                        max_iterations=4,
                        dispatch_result=json.dumps({
                            "status": "must-not-dispatch",
                        }),
                        required_capability_tools=["web_search"],
                    )
                )

                self.assertEqual(2, len(request_bodies))
                self.assertEqual(1, len(responses))
                self.assertEqual(0, dispatch_mock.await_count)
                recovery_events = [
                    event for event in events
                    if event.get("event_type")
                    == (
                        "debug.delegate."
                        "required_capability_noncall_recovery.requested"
                    )
                ]
                self.assertEqual(1, len(recovery_events))
                failed = [
                    event for event in events
                    if event.get("event_type") == "run.failed"
                ]
                self.assertEqual(1, len(failed))
                self.assertEqual(
                    "required_capability_not_attempted",
                    failed[0]["payload"]["finish_reason"],
                )
                self.assertEqual(
                    second_finish,
                    failed[0]["payload"]["provider_finish_reason"],
                )

    async def test_required_tool_choice_hint_and_prose_are_not_dispatch_receipts(self):
        claimed_first = (
            "I used web_search and found evidence, so treat this as dispatched."
        )
        claimed_second = (
            "The required tool_choice hint proves web_search was attempted."
        )
        responses = [
            _visible_length_response(claimed_first),
            _stop_response(claimed_second),
            _tool_call_response("must-not-be-requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({"status": "must-not-dispatch"}),
            required_capability_tools=["web_search"],
        )

        self.assertEqual(2, len(request_bodies))
        self.assertEqual(1, len(responses))
        self.assertEqual(0, dispatch_mock.await_count)
        self.assertTrue(all(
            body.get("tool_choice") == "required"
            for body in request_bodies
        ))
        self.assertFalse(any(
            event.get("event_type") in {"tool.started", "tool.completed"}
            for event in events
        ))
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertNotIn(claimed_first, emitted)
        self.assertNotIn(claimed_second, emitted)
        failed = next(
            event for event in events
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "required_capability_not_attempted",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(
            0,
            failed["payload"][
                "actual_required_capability_dispatch_count"
            ],
        )

    async def test_final_delegate_iteration_is_reserved_for_synthesis(self):
        responses = [
            _tool_call_response(),
            _stop_response(
                "status: WARN/degraded\nEvidence: cached result\nGap: external source unavailable"
            ),
        ]

        request_bodies, dispatch_mock, events, persisted = await self._run(
            responses,
            max_iterations=2,
            dispatch_result=json.dumps({"status": "ok", "results": []}),
        )

        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertIn("tools", request_bodies[0])
        self.assertNotIn("tools", request_bodies[1])
        final_messages = request_bodies[1]["messages"]
        self.assertTrue(any(
            message.get("role") == "user"
            and "final synthesis turn" in str(message.get("content"))
            for message in final_messages
        ))
        synthesis_debug = [
            event for event in events
            if event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get("delegate_forced_synthesis")
        ]
        self.assertEqual(len(synthesis_debug), 1)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})
        persisted_types = [event.get("event_type") for event in persisted]
        self.assertIn("run.started", persisted_types)
        self.assertIn("run.completed", persisted_types)

    async def test_reasoning_cycle_is_abandoned_and_recovers_once(self):
        cycle = (
            "Plan the bounded query, reconsider the same URL, and make the "
            "native capability call before returning the typed result. "
        ) * 12
        responses = [
            _reasoning_length_response(cycle * 12)[:-2],
            _stop_response(
                "status: WARN/degraded\n"
                "Evidence: no capability result was returned.\n"
                "Gap: the cyclic provider turn was discarded."
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=3,
            dispatch_result=json.dumps({"status": "ok"}),
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(dispatch_mock.await_count, 0)
        self.assertEqual(
            request_bodies[1].get("chat_template_kwargs"),
            {"enable_thinking": False},
        )
        aborts = [
            event for event in events
            if event.get("event_type")
            == "debug.llm.stream_convergence_aborted"
        ]
        self.assertEqual(len(aborts), 1)
        self.assertEqual(aborts[0]["payload"]["reason"], "reasoning_cycle_detected")
        self.assertFalse(any(
            event.get("type") == "reasoning_delta"
            for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_reasoning_cycle_on_recovery_is_terminal(self):
        cycle = (
            "Repeat the same bounded planning loop without making a call. "
        ) * 20
        cyclic_response = _reasoning_length_response(cycle * 15)[:-2]
        responses = [
            list(cyclic_response),
            list(cyclic_response),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({"status": "ok"}),
        )

        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        failed = next(
            event for event in events
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            failed["payload"]["finish_reason"],
            "provider_stream_convergence_failed",
        )
        self.assertEqual(events[-1]["type"], "error")

    async def test_raw_protocol_cycle_after_dispatch_is_discarded_before_recovery(self):
        contaminated = (
            "This provider turn must not cross the child boundary."
            + (
                "<tool_call>execute_code`\n"
                "code={\"b\":1}\n`\nlanguage=python\n"
            ) * 4
        )
        clean = (
            "status: WARN/degraded\n"
            "Evidence: retain only the earlier structured result.\n"
            "Gap: the malformed provider turn was discarded."
        )
        responses = [
            _tool_call_response("call-before-cycle"),
            _stop_response(contaminated),
            _stop_response(clean),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertNotIn("tools", request_bodies[2])
        self.assertEqual(
            request_bodies[2].get("chat_template_kwargs"),
            {"enable_thinking": False},
        )
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertNotIn("<tool_call>", emitted)
        self.assertIn(clean, emitted)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_primary_raw_protocol_discussion_keeps_direct_stream_semantics(self):
        discussion = (
            "A documentation example may literally discuss this spelling: "
            + "<tool_call>example` code={} ` " * 4
        )
        responses = [_stop_response(discussion)]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=1,
            dispatch_result=json.dumps({"status": "ok"}),
            delegated=False,
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        self.assertFalse(any(
            event.get("event_type")
            == "debug.llm.stream_convergence_aborted"
            for event in events
        ))
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(emitted, discussion)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_larger_delegate_reserves_two_no_tool_synthesis_turns(self):
        partial = (
            "status: WARN/degraded\nEvidence: bounded sources\n"
            "RESULT_FIELDS_JSON: {\"evidence\": "
        )
        responses = [
            _tool_call_response("call-search-1"),
            _tool_call_response("call-search-2"),
            _visible_length_response(partial),
            _stop_response(
                "RESULT_FIELDS_JSON: {\"evidence\": {\"status\": "
                "\"present\", \"value_summary\": \"bounded sources\", "
                "\"provenance\": \"search\"}}"
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({
                "status": "ok",
                "results": [{"title": "bounded evidence"}],
            }),
            required_result_fields=["evidence"],
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 4)
        self.assertEqual(dispatch_mock.await_count, 2)
        self.assertTrue(all("tools" in body for body in request_bodies[:2]))
        self.assertTrue(all("tools" not in body for body in request_bodies[2:]))
        synthesis_iterations = [
            event["payload"]["iteration"]
            for event in events
            if event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get("delegate_forced_synthesis")
        ]
        self.assertEqual(synthesis_iterations, [3, 4])
        final_messages = request_bodies[3]["messages"]
        self.assertTrue(any(
            message.get("role") == "assistant"
            and message.get("content") == partial
            for message in final_messages
        ))
        iteration_debug = next(
            event["payload"] for event in events
            if event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get("iteration") == 4
        )
        self.assertTrue(
            iteration_debug[
                "delegate_synthesis_length_continuation_discard_invalid_tail"
            ]
        )
        self.assertTrue(any(
            message.get("role") == "user"
            and "exact terminal typed footer" in str(message.get("content"))
            for message in final_messages
        ))
        continuation_events = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_synthesis_length_continuation"
        ]
        self.assertEqual(len(continuation_events), 1)
        self.assertEqual(continuation_events[0]["payload"]["remaining_iterations"], 1)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_length_completion_at_budget_edge_still_finalizes_footer(self):
        prefix = (
            "# Evidence\n"
            + "Retained source-backed finding with provenance. " * 40
        )
        suffix = (
            "\n## Conclusion\n"
            "The bounded evidence supports the stated finding; unavailable "
            "sources remain explicit gaps."
        )
        responses = [
            _tool_call_response("call-edge-search-1"),
            _tool_call_response("call-edge-search-2"),
            _visible_length_response(prefix),
            _stop_response(suffix),
            _stop_response(_result_fields_footer("evidence")),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({
                "status": "ok",
                "results": [{"title": "bounded evidence"}],
            }),
            required_result_fields=["evidence"],
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 5)
        self.assertEqual(dispatch_mock.await_count, 2)
        self.assertTrue(all("tools" not in body for body in request_bodies[2:4]))
        self.assertEqual(
            request_bodies[4]["tools"][0]["function"]["name"],
            "submit_result_fields",
        )
        requested = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
        )
        self.assertEqual(
            requested["payload"]["origin"],
            "synthesis_length_continuation",
        )
        self.assertTrue(requested["payload"]["main_iteration_budget_exhausted"])
        self.assertTrue(requested["payload"]["finalization_slot_borrowed"])
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(emitted.count("# Evidence"), 1)
        self.assertEqual(emitted.count("## Conclusion"), 1)
        self.assertEqual(emitted.count("RESULT_FIELDS_JSON:"), 1)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_synthesis_length_continuation_is_bounded_to_one(self):
        responses = [
            _tool_call_response("call-bounded-1"),
            _tool_call_response("call-bounded-2"),
            _visible_length_response(
                "status: WARN/degraded\nEvidence: partial synthesis"
            ),
            _visible_length_response(
                "\nGap: source unavailable\nRESULT_FIELDS_JSON: {"
            ),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({"status": "ok", "results": []}),
        )

        self.assertEqual(len(request_bodies), 4)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 2)
        self.assertTrue(all("tools" not in body for body in request_bodies[2:]))
        continuation_events = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_synthesis_length_continuation"
        ]
        self.assertEqual(len(continuation_events), 1)
        terminal_events = [
            event for event in events
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(len(terminal_events), 1)
        self.assertEqual(terminal_events[0]["payload"]["finish_reason"], "length")
        self.assertEqual(events[-1]["type"], "error")

    async def test_ordinary_visible_length_after_dispatch_completes_once_without_tools(self):
        partial = (
            "# Findings\nstatus: WARN/degraded\n"
            "Evidence: registry result R-1 with bounded provenance.\n"
            "The remaining conclusion was truncated"
        )
        completion = (
            "Conclusion: retain WARN because the secondary source was unavailable.\n"
            "Gap: secondary source unavailable.\n"
            + _result_fields_footer("study_id", "endpoint")
        )
        responses = [
            _tool_call_response("call-visible-length-evidence"),
            _visible_length_response(partial),
            _stop_response(completion),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result=json.dumps({
                "status": "ok",
                "results": [{"study_id": "R-1", "endpoint": "change"}],
            }),
            required_result_fields=["study_id", "endpoint"],
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertIn("tools", request_bodies[0])
        self.assertIn("tools", request_bodies[1])
        self.assertNotIn("tools", request_bodies[2])
        recovery_messages = request_bodies[2]["messages"]
        self.assertTrue(any(
            message.get("role") == "assistant"
            and message.get("content") == partial
            for message in recovery_messages
        ))
        recovery_prompt = next(
            str(message.get("content"))
            for message in recovery_messages
            if message.get("role") == "user"
            and "single bounded completion turn"
            in str(message.get("content"))
        )
        self.assertIn("no tools are available", recovery_prompt)
        self.assertIn("Do not repeat", recovery_prompt)
        self.assertIn("do not add new facts", recovery_prompt)
        self.assertIn("study_id", recovery_prompt)
        self.assertIn("endpoint", recovery_prompt)
        recovery_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
        ]
        self.assertEqual(len(recovery_gates), 1)
        self.assertEqual(
            recovery_gates[0]["payload"]["dispatched_tool_result_count"],
            1,
        )
        iteration_debug = {
            event["payload"]["iteration"]: event["payload"]
            for event in events
            if event.get("event_type") == "debug.iteration.started"
        }
        self.assertFalse(iteration_debug[2]["delegate_forced_synthesis"])
        self.assertIsNone(iteration_debug[2]["workflow_forced_tools"])
        self.assertTrue(
            iteration_debug[3]["delegate_visible_length_recovery"]
        )
        self.assertFalse(iteration_debug[3]["delegate_forced_synthesis"])
        self.assertEqual(iteration_debug[3]["workflow_forced_tools"], [])
        completion_debug = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.visible_length_recovery.completed"
        )
        self.assertTrue(completion_debug["payload"]["footer_valid"])
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertIn(partial + "\n" + completion, emitted)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_visible_length_with_empty_workflow_policy_recovers_once(self):
        reason = "bounded delegated workflow synthesis after evidence dispatch"

        def needs_no_tool_workflow_turn(run_state):
            if (
                run_state.successful_search_count >= 1
                and reason not in run_state.continuation_reasons
            ):
                return True, reason
            return False, ""

        partial = (
            "status: WARN/degraded\n"
            "Evidence: the bounded search result was retained.\n"
            "Gap: the conclusion was truncated"
        )
        completion = (
            "Conclusion: preserve the bounded evidence and explicit gap.\n"
            + _result_fields_footer("evidence_status", degraded=True)
        )
        responses = [
            _tool_call_response("call-empty-policy-visible-length"),
            _visible_length_response(partial),
            _stop_response(completion),
        ]

        with (
            patch(
                "agent_loop.HarnessRunState.needs_more_skill_workflow",
                new=needs_no_tool_workflow_turn,
            ),
            patch(
                "agent_loop._workflow_gate_tool_policy",
                return_value={
                    "tools": [],
                    "max_calls": 0,
                    "reason": reason,
                },
            ),
        ):
            request_bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=6,
                dispatch_result=json.dumps({
                    "status": "ok",
                    "results": [{"evidence_status": "bounded"}],
                }),
                required_result_fields=["evidence_status"],
            )

        origin_iteration = next(
            event["payload"] for event in events
            if event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get("iteration") == 2
        )
        self.assertEqual(origin_iteration["workflow_forced_tools"], [])
        self.assertFalse(origin_iteration["delegate_forced_synthesis"])
        self.assertNotIn("tools", request_bodies[1])
        recovery_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
        ]
        self.assertEqual(len(recovery_gates), 1)
        self.assertEqual(
            recovery_gates[0]["payload"]["tools_exposed_next_turn"],
            0,
        )
        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertNotIn("tools", request_bodies[2])
        self.assertFalse(responses)
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertIn(partial + "\n" + completion, emitted)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_visible_length_raw_protocol_after_dispatch_regenerates_once(self):
        contaminated = (
            "status: WARN/degraded\n"
            "Evidence: a bounded search result already returned.\n"
            "<tool_call><name>web_search</name>"
            "<arguments>{\"query\":\"must not dispatch\"}</arguments>"
            "</tool_call>\n"
            "Gap: this entire truncated turn is contaminated"
        )
        clean_result = (
            "status: WARN/degraded\n"
            "Evidence: use only the already-returned bounded result.\n"
            "Gap: the later pseudo call was discarded without dispatch.\n"
            + _result_fields_footer("evidence_status", degraded=True)
        )
        responses = [
            _tool_call_response("call-before-raw-length"),
            _visible_length_response(contaminated),
            _stop_response(clean_result),
        ]
        turn_boundaries = []

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result=json.dumps({
                "status": "ok",
                "results": [{"evidence_status": "bounded"}],
            }),
            required_result_fields=["evidence_status"],
            turn_boundary_events=turn_boundaries,
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertIn("tools", request_bodies[0])
        self.assertIn("tools", request_bodies[1])
        self.assertNotIn("tools", request_bodies[2])
        recovery_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
        ]
        self.assertEqual(len(recovery_gates), 1)
        self.assertEqual(
            recovery_gates[0]["payload"]["reason"],
            "provider_output_limit_with_raw_tool_protocol",
        )
        self.assertEqual(
            recovery_gates[0]["payload"]["raw_tool_protocol_count"],
            1,
        )
        self.assertEqual(
            recovery_gates[0]["payload"]["current_turn_dispatch_count"],
            0,
        )
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ))
        iteration_two_finishes = [
            event for event in turn_boundaries
            if event.get("phase") == "finished"
            and event.get("iteration") == 2
        ]
        self.assertTrue(iteration_two_finishes)
        self.assertEqual(
            iteration_two_finishes[-1]["finish_reason"],
            "abandoned",
        )
        self.assertEqual(
            iteration_two_finishes[-1]["abandon_reason"],
            "output_limit_with_raw_tool_protocol",
        )
        recovery_history = json.dumps(
            request_bodies[2]["messages"],
            ensure_ascii=False,
        )
        self.assertNotIn(contaminated, recovery_history)
        self.assertNotIn("must not dispatch", recovery_history)
        self.assertIn("Earlier tool results", recovery_history)
        completed = next(
            event for event in events
            if event.get("event_type") == "run.completed"
        )
        self.assertEqual(
            completed["payload"]["terminal_reason"],
            "post_dispatch_stream_recovery_synthesis",
        )
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_visible_length_recovery_second_length_is_terminal(self):
        responses = [
            _tool_call_response("call-visible-length-bounded"),
            _visible_length_response(
                "status: WARN\nEvidence: retained partial result"
            ),
            _visible_length_response(
                "Conclusion still truncated before the typed footer"
            ),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertNotIn("tools", request_bodies[2])
        self.assertEqual(sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ), 1)
        self.assertFalse(any(
            event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
            for event in events
        ))
        failed = next(
            event for event in events
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            failed["payload"]["finish_reason"],
            "delegated_visible_length_recovery_failed",
        )
        self.assertEqual(failed["payload"]["provider_finish_reason"], "length")
        self.assertEqual(events[-1]["type"], "error")

    async def test_visible_length_recovery_empty_stop_is_terminal(self):
        responses = [
            _tool_call_response("call-visible-length-empty"),
            _visible_length_response("status: WARN\nEvidence: retained"),
            _stop_response(""),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
        )

        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 1)
        failed_debug = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.visible_length_recovery.failed"
        )
        self.assertEqual(failed_debug["payload"]["reason"], "empty_visible_result")
        self.assertEqual(events[-1]["type"], "error")

    async def test_visible_length_recovery_rejects_tool_protocol_without_dispatch(self):
        responses = [
            _tool_call_response("call-visible-length-initial"),
            _visible_length_response("status: WARN\nEvidence: retained"),
            _tool_call_response("call-must-not-dispatch"),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
        )

        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertNotIn("tools", request_bodies[2])
        failed = next(
            event for event in events
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            failed["payload"]["finish_reason"],
            "delegated_visible_length_recovery_protocol_invalid",
        )
        self.assertEqual(events[-1]["type"], "error")

    async def test_visible_recovery_invalid_footer_gets_one_footer_only_repair(self):
        retained_prefix = (
            "# Findings\n"
            + "PASS: bounded evidence with explicit provenance. " * 700
        )[:30_000]
        retained_suffix = (
            "Conclusion detail remains bounded by the returned evidence. "
            * 350
        )[:17_000]
        malformed_footer = 'RESULT_FIELDS_JSON: {"wrong_key":'
        valid_footer = _result_fields_footer("study_id")
        responses = [
            _tool_call_response("call-visible-length-footer"),
            _visible_length_response(retained_prefix),
            _stop_response(retained_suffix + "\n" + malformed_footer),
            _result_fields_submit_response("study_id"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(retained_prefix), 30_000)
        self.assertEqual(len(retained_suffix), 17_000)
        self.assertEqual(len(request_bodies), 4)
        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertEqual(sum(
            event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
            for event in events
        ), 1)
        self.assertEqual(
            request_bodies[3]["tools"][0]["function"]["name"],
            "submit_result_fields",
        )
        self.assertEqual(request_bodies[3]["tool_choice"], "required")
        self.assertFalse(request_bodies[3]["parallel_tool_calls"])
        self.assertEqual(request_bodies[3]["temperature"], 0)
        self.assertEqual(
            request_bodies[3].get("chat_template_kwargs"),
            {"enable_thinking": False},
        )
        repair_messages = request_bodies[3]["messages"]
        self.assertEqual(["system", "user"], [
            message.get("role") for message in repair_messages
        ])
        repair_payload = json.loads(repair_messages[1]["content"])
        self.assertEqual(
            retained_suffix + "\n",
            repair_payload["retained_substantive_result"],
        )
        self.assertFalse(any(
            malformed_footer in str(message.get("content") or "")
            for message in repair_messages
            if message.get("role") == "assistant"
        ))
        completion = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.completed"
        )
        self.assertTrue(completion["payload"]["footer_valid"])
        self.assertEqual(
            completion["payload"]["origin"],
            "visible_length_recovery",
        )
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(emitted.count(retained_prefix), 1)
        self.assertEqual(emitted.count(retained_suffix), 1)
        self.assertNotIn(malformed_footer, emitted)
        self.assertEqual(emitted.count("RESULT_FIELDS_JSON:"), 1)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_visible_recovery_replaces_prefix_ledger_without_extra_turn(self):
        prefix_body = (
            "# Evidence\n"
            + "PASS: retained bounded evidence with explicit provenance. " * 12
        )
        old_valid_footer = _result_fields_footer("study_id")
        malformed_footer = 'RESULT_FIELDS_JSON: {"study_id":'
        replacement_footer = _result_fields_footer("study_id", degraded=True)
        stale_completion = (
            '"status":"present","value_summary":"stale tail"}}\n'
            "Conclusion: stale ledger continuation must not be retained."
        )

        for label, prefix_footer in (
            ("malformed", malformed_footer),
            ("valid_but_nonterminal", old_valid_footer),
        ):
            responses = [
                _tool_call_response(f"call-prefix-ledger-{label}"),
                _visible_length_response(
                    prefix_body + "\n" + prefix_footer
                ),
                _stop_response(
                    stale_completion + "\n" + replacement_footer
                ),
                _stop_response("must not be requested"),
            ]
            with self.subTest(prefix_footer=label), patch(
                "agent_loop._DELEGATE_SYNTHESIS_TURN_RESERVE",
                1,
            ):
                request_bodies, dispatch_mock, events, _persisted = (
                    await self._run(
                        responses,
                        max_iterations=3,
                        dispatch_result=json.dumps({
                            "status": "ok",
                            "results": [1],
                        }),
                        required_result_fields=["study_id"],
                    )
                )

            self.assertEqual(len(request_bodies), 3)
            self.assertEqual(len(responses), 1)
            self.assertEqual(dispatch_mock.await_count, 1)
            self.assertFalse(any(
                event.get("event_type")
                == "debug.delegate.result_footer_repair.requested"
                for event in events
            ))
            completed = next(
                event for event in events
                if event.get("event_type")
                == "debug.delegate.visible_length_recovery.completed"
            )
            self.assertTrue(completed["payload"]["footer_valid"])
            emitted = "".join(
                str(event.get("content") or "")
                for event in events
                if event.get("type") == "delta"
            )
            self.assertNotIn(stale_completion, emitted)
            self.assertEqual(1, emitted.count(replacement_footer))
            self.assertEqual(
                events[-1],
                {"type": "done", "finish_reason": "stop"},
            )

    async def test_prefix_ledger_invalid_completion_uses_only_footer_bridge(self):
        prefix_body = (
            "# Evidence\n"
            + "PASS: clean body remains bounded and attributable. " * 10
        )
        prefix = prefix_body + '\nRESULT_FIELDS_JSON: {"study_id":'
        valid_footer = _result_fields_footer("study_id")

        for label, stale_completion in (
            ("missing_marker", "stale JSON tail and conclusion"),
            (
                "malformed_marker",
                "stale JSON tail\nRESULT_FIELDS_JSON: {\n  \"study_id\":",
            ),
        ):
            responses = [
                _tool_call_response(f"call-prefix-bridge-{label}"),
                _visible_length_response(prefix),
                _stop_response(stale_completion),
                _result_fields_submit_response("study_id"),
                _stop_response("must not be requested"),
            ]
            with self.subTest(completion=label), patch(
                "agent_loop._DELEGATE_SYNTHESIS_TURN_RESERVE",
                1,
            ):
                request_bodies, dispatch_mock, events, _persisted = (
                    await self._run(
                        responses,
                        max_iterations=4,
                        dispatch_result=json.dumps({
                            "status": "ok",
                            "results": [1],
                        }),
                        required_result_fields=["study_id"],
                    )
                )

            self.assertEqual(len(request_bodies), 4)
            self.assertEqual(len(responses), 1)
            self.assertEqual(dispatch_mock.await_count, 1)
            self.assertEqual(1, sum(
                event.get("event_type")
                == "debug.delegate.result_footer_repair.requested"
                for event in events
            ))
            emitted = "".join(
                str(event.get("content") or "")
                for event in events
                if event.get("type") == "delta"
            )
            self.assertNotIn(stale_completion, emitted)
            canonical_footer = "RESULT_FIELDS_JSON: " + json.dumps(
                _result_fields_arguments("study_id"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.assertEqual(1, emitted.count(canonical_footer))
            self.assertEqual(
                events[-1],
                {"type": "done", "finish_reason": "stop"},
            )

    async def test_visible_recovery_uses_independent_footer_finalizer(self):
        malformed = 'RESULT_FIELDS_JSON: {"wrong_key":'
        responses = [
            _tool_call_response("call-visible-footer-no-budget"),
            _visible_length_response("status: PASS\nEvidence: retained"),
            _stop_response("Conclusion retained.\n" + malformed),
            _stop_response(_result_fields_footer("study_id")),
        ]

        # Use a one-turn synthesis reserve to exercise the exact production
        # failure where visible recovery consumes the final ordinary
        # iteration. The isolated footer validator owns one independent slot.
        with patch("agent_loop._DELEGATE_SYNTHESIS_TURN_RESERVE", 1):
            request_bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=3,
                dispatch_result=json.dumps({"status": "ok", "results": [1]}),
                required_result_fields=["study_id"],
            )

        self.assertEqual(len(request_bodies), 4)
        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 1)
        requested = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
        )
        self.assertTrue(requested["payload"]["finalization_slot_borrowed"])
        self.assertEqual(
            requested["payload"]["finalization_budget_kind"],
            "independent_output_validation",
        )
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertNotIn(malformed, emitted)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_visible_recovery_footer_repair_failure_is_terminal(self):
        cases = {
            "invalid": _stop_response(_result_fields_footer("wrong_key")),
            "length": _visible_length_response("RESULT_FIELDS_JSON: {"),
            "empty": _stop_response(""),
            "raw_protocol": _stop_response(
                "<tool_call><name>web_search</name>"
                "<arguments>{}</arguments></tool_call>\n"
                + _result_fields_footer("study_id")
            ),
            "tool_fragment": _tool_call_response(
                "call-footer-must-not-dispatch"
            ),
        }
        for label, repair_response in cases.items():
            responses = [
                _tool_call_response(f"call-footer-terminal-{label}"),
                _visible_length_response(
                    "status: PASS\nEvidence: retained bounded result"
                ),
                _stop_response(
                    "Conclusion retained.\n"
                    'RESULT_FIELDS_JSON: {"study_id":'
                ),
                repair_response,
                _stop_response("must not be requested"),
            ]
            with self.subTest(label=label):
                request_bodies, dispatch_mock, events, _persisted = (
                    await self._run(
                        responses,
                        max_iterations=6,
                        dispatch_result=json.dumps({
                            "status": "ok",
                            "results": [1],
                        }),
                        required_result_fields=["study_id"],
                    )
                )

                self.assertEqual(len(request_bodies), 4)
                self.assertEqual(len(responses), 1)
                self.assertEqual(dispatch_mock.await_count, 1)
                self.assertEqual(sum(
                    event.get("event_type")
                    == "debug.delegate.result_footer_repair.requested"
                    for event in events
                ), 1)
                self.assertEqual(sum(
                    event.get("event_type")
                    == "debug.delegate.result_footer_repair.failed"
                    for event in events
                ), 1)
                self.assertFalse(any(
                    event.get("event_type")
                    == "debug.delegate.output_contract_repair.requested"
                    for event in events
                ))
                self.assertEqual(events[-1]["type"], "error")

    async def test_visible_recovery_raw_protocol_never_reaches_footer_repair(self):
        raw_protocol = (
            "<tool_call><name>web_search</name>"
            "<arguments>{}</arguments></tool_call>"
        )
        responses = [
            _tool_call_response("call-visible-raw-protocol"),
            _visible_length_response("status: PASS\nEvidence: retained"),
            _stop_response(raw_protocol),
            _stop_response(_result_fields_footer("study_id")),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertFalse(any(
            event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
            for event in events
        ))
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertNotIn(raw_protocol, emitted)
        self.assertEqual(events[-1]["type"], "error")

    async def test_visible_length_with_dispatch_but_no_budget_is_not_recovered(self):
        responses = [
            _tool_call_response("call-visible-length-no-budget"),
            _visible_length_response("status: WARN\nEvidence: bounded partial"),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=2,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
        )

        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ))
        self.assertEqual(events[-1]["type"], "error")

    async def test_missing_typed_footer_gets_one_in_budget_no_tool_repair(self):
        body = (
            "# Findings\nstatus: PASS\nEvidence: bounded registry and literature "
            "results with provenance.\nGap: none. " * 10
        ).rstrip()
        footer = _result_fields_footer("study_id", "endpoint")
        responses = [
            _tool_call_response("call-footer-evidence"),
            _stop_response(body),
            _result_fields_submit_response("study_id", "endpoint"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result=json.dumps({
                "status": "ok",
                "results": [{"study_id": "S-1", "endpoint": "change"}],
            }),
            required_result_fields=["study_id", "endpoint"],
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertIn("tools", request_bodies[0])
        self.assertIn("tools", request_bodies[1])
        self.assertEqual(
            request_bodies[2]["tools"][0]["function"]["name"],
            "submit_result_fields",
        )
        repair_messages = request_bodies[2]["messages"]
        self.assertEqual(["system", "user"], [
            message.get("role") for message in repair_messages
        ])
        repair_payload = json.loads(repair_messages[1]["content"])
        self.assertEqual(body, repair_payload["retained_substantive_result"])
        self.assertEqual(
            ["study_id", "endpoint"],
            repair_payload["required_result_fields"],
        )
        repair_iterations = [
            event["payload"]
            for event in events
            if event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get(
                "delegate_result_footer_repair"
            )
        ]
        self.assertEqual(len(repair_iterations), 1)
        self.assertEqual(repair_iterations[0]["iteration"], 3)
        self.assertEqual(repair_iterations[0]["remaining_after_consume"], 2)
        continuation_events = [
            event
            for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_result_footer_repair"
        ]
        self.assertEqual(len(continuation_events), 1)
        content = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertIn(body, content)
        self.assertEqual(content.count("RESULT_FIELDS_JSON:"), 1)
        submitted = json.loads(
            content.rsplit("RESULT_FIELDS_JSON: ", 1)[1]
        )
        self.assertEqual(set(submitted), {"study_id", "endpoint"})
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_complex_footer_repair_uses_same_32k_contract_tier(self):
        fields = [f"field_{index}" for index in range(22)]
        responses = [
            _stop_response(
                "# Structured evidence\n"
                "The retained result supports each declared bounded field."
            ),
            _result_fields_submit_response(*fields),
        ]
        schema = {
            field: {"type": "object"}
            for field in fields
        }

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=3,
            dispatch_result=json.dumps({"status": "unused"}),
            required_result_fields=fields,
            required_result_schema=schema,
            max_tokens=32_768,
        )

        self.assertFalse(responses)
        self.assertEqual(2, len(request_bodies))
        self.assertEqual(32_768, request_bodies[1]["max_tokens"])
        self.assertEqual(
            "submit_result_fields",
            request_bodies[1]["tools"][0]["function"]["name"],
        )
        self.assertEqual(0, dispatch_mock.await_count)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_footer_submitter_corrupt_stream_uses_one_nonstream_transport(self):
        arguments = json.dumps(
            _result_fields_arguments("study_id"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        replacement = ToolCallStreamAssembly(
            calls=(AssembledStreamToolCall(
                call_id="replacement-submit",
                name="submit_result_fields",
                arguments=arguments,
            ),),
            errors=(),
            debug={"argument_chars_total": len(arguments)},
        )
        nonstream = AsyncMock(return_value=(
            replacement,
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            {"replacement": "valid"},
        ))
        responses = [
            _stop_response(
                "# Findings\nEvidence: retained bounded result without footer."
            ),
            _partial_corrupt_tool_response(content=""),
        ]

        with patch(
            "agent_loop._request_openai_nonstream_tool_call_fallback",
            nonstream,
        ):
            request_bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=2,
                dispatch_result=json.dumps({"status": "unused"}),
                required_result_fields=["study_id"],
            )

        self.assertEqual(2, len(request_bodies))
        self.assertEqual(1, nonstream.await_count)
        self.assertEqual(0, dispatch_mock.await_count)
        self.assertTrue(any(
            event.get("event_type")
            == "debug.tool.stream.continuation_repair.transport_fallback.completed"
            for event in events
        ))
        completed = next(
            event for event in events
            if event.get("event_type") == "run.completed"
        )
        self.assertEqual(
            "delegated_result_footer_structured_repair",
            completed["payload"]["terminal_reason"],
        )
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_early_stream_repair_does_not_poison_final_footer_repair(self):
        body = (
            "# Findings\nstatus: PASS\n"
            "Evidence: nine bounded tool results with explicit provenance.\n"
            "Gap: none in the retained evidence body.\n"
            'RESULT_FIELDS_JSON: {"study_id":'
        )
        footer = _result_fields_footer("study_id", "endpoint")
        tool_turns = [
            _tool_call_response(
                f"call-evidence-{index}",
                arguments={"query": f"bounded evidence {index}"},
            )
            for index in range(1, 10)
        ]
        responses = [
            _partial_corrupt_tool_response(),
            *tool_turns,
            _stop_response(body),
            _result_fields_submit_response("study_id", "endpoint"),
        ]
        dispatch_results = [
            json.dumps({
                "status": "ok",
                "results": [{"evidence_id": f"E-{index}"}],
            })
            for index in range(1, 10)
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=12,
            dispatch_result=json.dumps({"status": "unused"}),
            dispatch_results=dispatch_results,
            required_result_fields=["study_id", "endpoint"],
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 12)
        self.assertEqual(dispatch_mock.await_count, 9)
        self.assertEqual(request_bodies[1].get("tool_choice"), "required")
        self.assertNotIn("tools", request_bodies[10])
        self.assertEqual(
            request_bodies[11]["tools"][0]["function"]["name"],
            "submit_result_fields",
        )
        self.assertEqual(
            request_bodies[10].get("chat_template_kwargs"),
            {"enable_thinking": False},
        )
        self.assertEqual(
            request_bodies[11].get("chat_template_kwargs"),
            {"enable_thinking": False},
        )
        repair_messages = request_bodies[11]["messages"]
        self.assertEqual(["system", "user"], [
            message.get("role") for message in repair_messages
        ])
        repair_payload = json.loads(repair_messages[1]["content"])
        self.assertNotIn(
            "RESULT_FIELDS_JSON:",
            repair_payload["retained_substantive_result"],
        )
        self.assertEqual(
            ["study_id", "endpoint"],
            repair_payload["required_result_fields"],
        )
        iterations = {
            event["payload"]["iteration"]: event["payload"]
            for event in events
            if event.get("event_type") == "debug.iteration.started"
        }
        self.assertTrue(iterations[11]["delegate_forced_synthesis"])
        self.assertEqual(iterations[11]["remaining_after_consume"], 1)
        self.assertEqual(iterations[11]["workflow_forced_tools"], [])
        self.assertTrue(iterations[12]["delegate_result_footer_repair"])
        self.assertEqual(iterations[12]["remaining_after_consume"], 0)
        self.assertEqual(sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "tool_stream_continuation_repair"
            for event in events
        ), 1)
        self.assertEqual(sum(
            event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
            for event in events
        ), 1)
        completed = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.completed"
        )
        self.assertTrue(completed["payload"]["footer_valid"])
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_footer_repair_after_stream_repair_nonstop_is_terminal(self):
        body = (
            "# Findings\nstatus: WARN\n"
            "Evidence: repaired tool result retained.\n"
            'RESULT_FIELDS_JSON: {"study_id":'
        )
        responses = [
            _partial_corrupt_tool_response(),
            _tool_call_response(
                "call-repaired-evidence",
                arguments={"query": "repaired evidence"},
            ),
            _stop_response(body),
            _visible_length_response('RESULT_FIELDS_JSON: {"study_id":'),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(request_bodies), 4)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertEqual(
            "submit_result_fields",
            request_bodies[3]["tools"][0]["function"]["name"],
        )
        self.assertEqual(sum(
            event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
            for event in events
        ), 1)
        failed = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.failed"
        )
        self.assertEqual(
            failed["payload"]["reason"],
            "structured_submitter_not_emitted",
        )
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate") in {
                "delegate_synthesis_length_continuation",
                "delegate_reasoning_only_length_recovery",
                "delegate_visible_length_recovery",
            }
            for event in events
        ))
        self.assertEqual(events[-1]["type"], "error")

    async def test_stream_repair_bad_footer_uses_finalization_slot(self):
        responses = [
            _partial_corrupt_tool_response(),
            _tool_call_response(
                "call-last-evidence",
                arguments={"query": "last bounded evidence"},
            ),
            _stop_response(
                "# Findings\nstatus: PASS\nEvidence: retained.\n"
                'RESULT_FIELDS_JSON: {"study_id":'
            ),
            _stop_response(_result_fields_footer("study_id")),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=3,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(request_bodies), 4)
        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 1)
        requested = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
        )
        self.assertTrue(requested["payload"]["finalization_slot_borrowed"])
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_valid_typed_footer_does_not_trigger_repair(self):
        body = "# Findings\nstatus: PASS\nEvidence: verified. " * 10
        footer = _result_fields_footer("study_id", degraded=True)
        responses = [_stop_response(body + "\n" + footer)]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=3,
            dispatch_result=json.dumps({"status": "ok"}),
            required_result_fields=["study_id"],
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        self.assertFalse(any(
            event.get("event_type", "").startswith(
                "debug.delegate.result_footer_repair"
            )
            for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_missing_footer_after_last_turn_uses_finalization_slot(self):
        responses = [
            _stop_response(
                "# Findings\nstatus: PASS\nEvidence: substantive but the "
                "typed footer is absent. " * 8
            ),
            _stop_response(_result_fields_footer("study_id")),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=1,
            dispatch_result=json.dumps({"status": "ok"}),
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(request_bodies), 2)
        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 0)
        requested = [
            event
            for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
        ]
        self.assertEqual(len(requested), 1)
        self.assertTrue(requested[0]["payload"]["finalization_slot_borrowed"])
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_invalid_footer_repair_is_not_repeated(self):
        responses = [
            _stop_response(
                "# Findings\nstatus: PASS\nEvidence: retained body without a "
                "footer. " * 8
            ),
            _stop_response(_result_fields_footer("wrong_key")),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result=json.dumps({"status": "ok"}),
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        self.assertEqual(
            "submit_result_fields",
            request_bodies[1]["tools"][0]["function"]["name"],
        )
        failures = [
            event
            for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.failed"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            failures[0]["payload"]["reason"],
            "structured_submitter_not_emitted",
        )
        self.assertEqual(events[-1]["type"], "error")

    async def test_footer_repair_accepts_exact_canonical_footer_text(self):
        body = (
            "# Findings\nstatus: WARN\nEvidence: bounded source attempt.\n"
            "Gap: the declared external source was unavailable."
        )
        responses = [
            _stop_response(body),
            _stop_response(_result_fields_footer("study_id", degraded=True)),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=3,
            dispatch_result=json.dumps({"status": "unused"}),
            required_result_fields=["study_id"],
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(dispatch_mock.await_count, 0)
        accepted = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.text_compatibility"
        )
        self.assertTrue(accepted["payload"]["accepted"])
        self.assertEqual(
            accepted["payload"]["method"],
            "canonical_footer_text",
        )
        content = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertIn(body, content)
        self.assertEqual(content.count("RESULT_FIELDS_JSON:"), 1)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_footer_repair_accepts_exact_bare_json_text(self):
        body = "# Findings\nEvidence: a bounded source result."
        responses = [
            _stop_response(body),
            _stop_response(json.dumps(
                _result_fields_arguments("study_id"),
                ensure_ascii=False,
                separators=(",", ":"),
            )),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=3,
            dispatch_result=json.dumps({"status": "unused"}),
            required_result_fields=["study_id"],
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(dispatch_mock.await_count, 0)
        accepted = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.text_compatibility"
        )
        self.assertTrue(accepted["payload"]["accepted"])
        self.assertEqual(accepted["payload"]["method"], "bare_json_text")
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_footer_repair_length_does_not_chain_other_recoveries(self):
        responses = [
            _stop_response(
                "# Findings\nstatus: PASS\nEvidence: retained body without a "
                "footer. " * 8
            ),
            _visible_length_response("RESULT_FIELDS_JSON: {"),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result=json.dumps({"status": "ok"}),
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate") in {
                "delegate_synthesis_length_continuation",
                "delegate_reasoning_only_length_recovery",
                "tool_stream_continuation_repair",
            }
            for event in events
        ))
        failed = [
            event
            for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(
            failed[0]["payload"]["reason"],
            "structured_submitter_not_emitted",
        )
        self.assertEqual(events[-1]["type"], "error")

    async def test_footer_repair_does_not_chain_after_reasoning_recovery(self):
        responses = [
            _reasoning_length_response(),
            _stop_response(
                "# Findings\nstatus: PASS\nEvidence: substantive body after the "
                "bounded reasoning recovery, but no footer. " * 8
            ),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({"status": "ok"}),
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        self.assertFalse(any(
            event.get("event_type")
            == "debug.delegate.result_footer_repair.requested"
            for event in events
        ))
        unavailable = [
            event
            for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.unavailable"
        ]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(
            unavailable[0]["payload"]["reason"],
            "incompatible_current_phase",
        )
        self.assertEqual(
            unavailable[0]["payload"]["incompatible_reasons"],
            ["reasoning_only_recovery_active"],
        )
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_primary_chat_ignores_internal_delegated_footer_contract(self):
        responses = [
            _stop_response("A normal direct answer without any typed footer."),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=3,
            dispatch_result=json.dumps({"status": "ok"}),
            delegated=False,
            required_result_fields=["study_id"],
        )

        self.assertEqual(len(request_bodies), 1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        self.assertFalse(any(
            "result_footer_repair" in str(event.get("event_type") or "")
            for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_identical_failed_capability_is_quarantined_after_three_calls(self):
        responses = [
            _tool_call_response("call-1"),
            _tool_call_response("call-2"),
            _tool_call_response("call-3"),
            _stop_response(
                "status: WARN/degraded\nEvidence: none\nGap: search provider unavailable"
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result=json.dumps({
                "status": "error",
                "error": "search provider unavailable",
            }),
        )

        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 3)
        self.assertTrue(all("tools" in body for body in request_bodies[:3]))
        self.assertNotIn("tools", request_bodies[3])
        quarantine_debug = [
            event for event in events
            if event.get("event_type") == "debug.tool.quarantined"
        ]
        self.assertEqual(len(quarantine_debug), 1)
        self.assertEqual(
            quarantine_debug[0]["payload"]["tools"],
            ["web_search"],
        )
        final_messages = request_bodies[3]["messages"]
        self.assertTrue(any(
            message.get("role") == "user"
            and "quarantined" in str(message.get("content"))
            for message in final_messages
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_delegated_synthesis_receives_complete_ten_call_machine_ledger(self):
        calls = [
            (
                f"call-ledger-{index}",
                self.fake_tool_names[index % len(self.fake_tool_names)],
                {"value": f"cross-domain request {index}"},
            )
            for index in range(10)
        ]
        responses = [
            _tool_calls_response(calls),
            _stop_response(
                "status: WARN/degraded\n"
                "Evidence: nine bounded capability calls succeeded.\n"
                "Gap: one bounded capability call failed."
            ),
        ]
        dispatch_results = [
            (
                json.dumps({
                    "status": "error",
                    "error": "bounded generic source unavailable",
                })
                if index == 6
                else json.dumps({
                    "status": "success",
                    "record": {"id": index, "value": f"evidence-{index}"},
                })
            )
            for index in range(10)
        ]

        with patch(
            "agent_loop.preflight_tool",
            side_effect=self._allow_fake_tool_preflight,
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=2,
                dispatch_result="unused",
                dispatch_results=dispatch_results,
                tools=list(self.fake_tool_names),
                schemas=self.fake_tool_schemas,
            )

        self.assertFalse(responses)
        self.assertEqual(10, dispatch_mock.await_count)
        self.assertEqual(2, len(bodies))
        self.assertNotIn("tools", bodies[1])
        synthesis_history = "\n".join(
            str(message.get("content") or "")
            for message in bodies[1]["messages"]
        )
        marker = (
            "[Harness machine-owned delegated capability invocation ledger]"
        )
        self.assertEqual(1, synthesis_history.count(marker))
        ledger_line = next(
            str(message.get("content") or "")
            for message in bodies[1]["messages"]
            if marker in str(message.get("content") or "")
        )
        raw_ledger = ledger_line.split(
            "CAPABILITY_INVOCATION_LEDGER_JSON: ", 1
        )[1].split(". This is the authoritative accounting", 1)[0]
        ledger = json.loads(raw_ledger)
        self.assertEqual(10, ledger["attempted"])
        self.assertEqual(9, ledger["succeeded"])
        self.assertEqual(1, ledger["failed"])
        self.assertEqual(0, ledger["pending"])
        self.assertFalse(ledger["ordered_calls_truncated"])
        self.assertEqual(10, len(ledger["ordered_calls"]))
        self.assertEqual(
            [f"cross-domain request {index}" for index in range(10)],
            [
                item["argument_summary"]["value"]
                for item in ledger["ordered_calls"]
            ],
        )
        self.assertEqual(
            ["succeeded"] * 6 + ["failed"] + ["succeeded"] * 3,
            [item["status"] for item in ledger["ordered_calls"]],
        )
        ledger_events = [
            event["payload"]
            for event in events
            if event.get("event_type")
            == "debug.capability.invocation_ledger"
        ]
        self.assertEqual(1, len(ledger_events))
        self.assertEqual(10, ledger_events[0]["attempted"])
        self.assertEqual(9, ledger_events[0]["succeeded"])
        self.assertEqual(1, ledger_events[0]["failed"])
        completed = next(
            event["payload"]
            for event in events
            if event.get("event_type") == "run.completed"
        )
        self.assertEqual(
            ledger,
            completed["capability_invocation_ledger"],
        )
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_rotating_failed_tools_exhaust_delegated_run_budget(self):
        tool_sequence = [
            self.fake_tool_names[index % len(self.fake_tool_names)]
            for index in range(6)
        ]
        responses = [
            _tool_call_response(
                f"call-cross-failure-{index}",
                tool_name=tool_name,
                arguments={"value": f"attempt-{index}"},
            )
            for index, tool_name in enumerate(tool_sequence)
        ] + [
            _stop_response(
                "status: WARN/degraded\nEvidence: none\n"
                "Gap: every bounded capability failed"
            ),
        ]

        with patch(
            "agent_loop.preflight_tool",
            side_effect=self._allow_fake_tool_preflight,
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=9,
                dispatch_result=json.dumps({
                    "status": "error",
                    "error": "generic provider unavailable",
                }),
                tools=list(self.fake_tool_names),
                schemas=self.fake_tool_schemas,
            )

        self.assertFalse(responses)
        self.assertEqual(6, dispatch_mock.await_count)
        self.assertTrue(all("tools" in body for body in bodies[:6]))
        self.assertNotIn("tools", bodies[6])
        streaks = [
            event["payload"]["consecutive_failure_count"]
            for event in events
            if event.get("event_type") == "debug.tool.failure_streak"
        ]
        self.assertEqual([1, 2, 3, 4, 5, 6], streaks)
        exhausted = next(
            event["payload"] for event in events
            if event.get("event_type")
            == "debug.tool.failure_budget_exhausted"
        )
        self.assertEqual(6, exhausted["limit"])
        self.assertEqual(6, exhausted["consecutive_failure_count"])
        self.assertEqual([], exhausted["remaining_tools"])
        self.assertEqual(
            sorted(self.fake_tool_names),
            exhausted["quarantined_tools"],
        )
        quarantine = next(
            event["payload"] for event in events
            if event.get("event_type") == "debug.tool.quarantined"
        )
        self.assertTrue(quarantine["cross_tool_failure_budget_exhausted"])
        self.assertEqual([], quarantine["failure_tools"])
        synthesis = next(
            event["payload"] for event in events
            if event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get(
                "delegate_cross_tool_failure_synthesis"
            )
        )
        self.assertTrue(synthesis["delegate_forced_synthesis"])
        self.assertEqual([], synthesis["workflow_forced_tools"])
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_substantive_success_resets_cross_tool_failure_streak(self):
        tool_sequence = [
            "fake_tool_a",
            "fake_tool_b",
            "fake_tool_c",
            "fake_tool_a",
            "fake_tool_b",
            "fake_tool_c",
        ]
        responses = [
            _tool_call_response(
                f"call-reset-{index}",
                tool_name=tool_name,
                arguments={"value": f"attempt-{index}"},
            )
            for index, tool_name in enumerate(tool_sequence)
        ] + [
            _stop_response(
                "status: WARN/degraded\nEvidence: one bounded result\n"
                "Gap: later providers unavailable"
            ),
        ]
        error_result = json.dumps({
            "status": "error",
            "error": "generic provider unavailable",
        })
        dispatch_results = [
            error_result,
            error_result,
            error_result,
            json.dumps({
                "status": "success",
                "records": [{"id": "evidence-1", "value": "bounded"}],
            }),
            error_result,
            error_result,
        ]

        with patch(
            "agent_loop.preflight_tool",
            side_effect=self._allow_fake_tool_preflight,
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=8,
                dispatch_result="unused",
                dispatch_results=dispatch_results,
                tools=list(self.fake_tool_names),
                schemas=self.fake_tool_schemas,
            )

        self.assertFalse(responses)
        self.assertEqual(6, dispatch_mock.await_count)
        streaks = [
            event["payload"]["consecutive_failure_count"]
            for event in events
            if event.get("event_type") == "debug.tool.failure_streak"
        ]
        self.assertEqual([1, 2, 3, 1, 2], streaks)
        reset = next(
            event["payload"] for event in events
            if event.get("event_type") == "debug.tool.failure_streak_reset"
        )
        self.assertEqual(3, reset["prior_consecutive_failure_count"])
        self.assertEqual("new_semantic_tool_result", reset["reset_reason"])
        self.assertFalse(any(
            event.get("event_type")
            == "debug.tool.failure_budget_exhausted"
            for event in events
        ))
        self.assertNotIn("tools", bodies[-1])

    async def test_identical_success_does_not_mask_cross_tool_failures(self):
        tool_sequence = [
            "fake_tool_a",
            "fake_tool_a",
            "fake_tool_b",
            "fake_tool_c",
            "fake_tool_a",
            "fake_tool_a",
            "fake_tool_b",
            "fake_tool_c",
        ]
        responses = [
            _tool_call_response(
                f"call-no-progress-{index}",
                tool_name=tool_name,
                arguments={"value": f"attempt-{index}"},
            )
            for index, tool_name in enumerate(tool_sequence)
        ] + [
            _stop_response(
                "status: WARN/degraded\nEvidence: repeated empty result\n"
                "Gap: all evidence capabilities failed"
            ),
        ]
        repeated_success = json.dumps({"status": "success", "records": []})
        error_result = json.dumps({
            "status": "error",
            "error": "generic provider unavailable",
        })
        dispatch_results = [
            repeated_success,
            error_result,
            error_result,
            error_result,
            repeated_success,
            error_result,
            error_result,
            error_result,
        ]

        with patch(
            "agent_loop.preflight_tool",
            side_effect=self._allow_fake_tool_preflight,
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=11,
                dispatch_result="unused",
                dispatch_results=dispatch_results,
                tools=list(self.fake_tool_names),
                schemas=self.fake_tool_schemas,
            )

        self.assertFalse(responses)
        self.assertEqual(8, dispatch_mock.await_count)
        streaks = [
            event["payload"]["consecutive_failure_count"]
            for event in events
            if event.get("event_type") == "debug.tool.failure_streak"
        ]
        self.assertEqual([1, 2, 3, 4, 5, 6], streaks)
        self.assertFalse(any(
            event.get("event_type") == "debug.tool.failure_streak_reset"
            for event in events
        ))
        self.assertTrue(any(
            event.get("event_type") == "debug.tool.no_progress_success"
            and event.get("payload", {}).get("identical_success_count") == 2
            for event in events
        ))
        self.assertNotIn("tools", bodies[-1])

    async def test_primary_run_has_no_cross_tool_failure_budget(self):
        tool_sequence = [
            self.fake_tool_names[index % len(self.fake_tool_names)]
            for index in range(6)
        ]
        responses = [
            _tool_call_response(
                f"call-primary-failure-{index}",
                tool_name=tool_name,
                arguments={"value": f"attempt-{index}"},
            )
            for index, tool_name in enumerate(tool_sequence)
        ]

        with (
            patch(
                "agent_loop.preflight_tool",
                side_effect=self._allow_fake_tool_preflight,
            ),
            patch(
                "agent_loop._direct_chat_tool_exposure",
                return_value=DirectToolExposure(
                    tools=self.fake_tool_names,
                    reasons=("generic root boundary test",),
                    required_groups=(),
                    missing_requirements=(),
                ),
            ),
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=6,
                dispatch_result=json.dumps({
                    "status": "error",
                    "error": "generic provider unavailable",
                }),
                tools=list(self.fake_tool_names),
                schemas=self.fake_tool_schemas,
                delegated=False,
            )

        self.assertFalse(responses)
        self.assertEqual(6, dispatch_mock.await_count)
        self.assertTrue(all("tools" in body for body in bodies))
        self.assertEqual(6, sum(
            event.get("event_type") == "tool.failed"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") in {
                "debug.tool.failure_streak",
                "debug.tool.failure_budget_exhausted",
                "debug.tool.quarantined",
            }
            for event in events
        ))
        self.assertEqual("error", events[-1]["type"])

    async def test_one_preflight_denial_allows_corrected_entrypoint_dispatch(self):
        tool_name = "run_skill_python"
        schema = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Run one exact granted Python entrypoint.",
                "parameters": {
                    "type": "object",
                    "properties": {"script_path": {"type": "string"}},
                    "required": ["script_path"],
                },
            },
        }]
        wrong = "skills/catalog-database/scripts/invented.py"
        correct = "skills/catalog-database/scripts/catalog_helper.py"
        responses = [
            _tool_call_response(
                "call-preflight-wrong",
                tool_name=tool_name,
                arguments={"script_path": wrong},
            ),
            _tool_call_response(
                "call-preflight-corrected",
                tool_name=tool_name,
                arguments={"script_path": correct},
            ),
            _stop_response(
                "status: PASS\nEvidence: exact granted entrypoint dispatched\n"
                "Gap: none"
            ),
        ]

        def boundary_error(name, args, context):
            if args.get("script_path") == wrong:
                return (
                    "Delegated resource boundary rejected run_skill_python "
                    "outside the compiled Skill/script capability closure."
                )
            return None

        with patch(
            "tools.registry.delegated_resource_boundary_error",
            side_effect=boundary_error,
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=4,
                dispatch_result=json.dumps({"status": "success"}),
                tools=[tool_name],
                schemas=schema,
                required_capability_tools=[tool_name],
                allowed_skill_scripts=[(
                    "catalog-database", "scripts/catalog_helper.py", "a" * 64,
                )],
            )

        self.assertFalse(responses)
        self.assertEqual(1, dispatch_mock.await_count)
        self.assertIn("tools", bodies[1])
        self.assertFalse(any(
            event.get("event_type") == "debug.tool.quarantined"
            for event in events
        ))
        terminal_calls = [
            event["payload"]
            for event in events
            if event.get("event_type") in {"tool.failed", "tool.completed"}
        ]
        self.assertFalse(terminal_calls[0]["actual_dispatch_attempted"])
        self.assertTrue(terminal_calls[1]["actual_dispatch_attempted"])

    async def test_same_iteration_duplicates_count_once_then_alternate_entrypoint_dispatches(self):
        tool_name = "run_skill_python"
        schema = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Run one exact granted Python entrypoint.",
                "parameters": {
                    "type": "object",
                    "properties": {"script_path": {"type": "string"}},
                    "required": ["script_path"],
                },
            },
        }]
        changed = "skills/catalog-database/scripts/changed_helper.py"
        alternate = "skills/catalog-database/scripts/alternate_helper.py"
        responses = [
            _tool_calls_response([
                (
                    "call-same-turn-a",
                    tool_name,
                    {"script_path": changed},
                ),
                (
                    "call-same-turn-b",
                    tool_name,
                    {"script_path": changed},
                ),
            ]),
            _tool_call_response(
                "call-next-turn-same",
                tool_name=tool_name,
                arguments={"script_path": changed},
            ),
            _tool_call_response(
                "call-third-turn-alternate",
                tool_name=tool_name,
                arguments={"script_path": alternate},
            ),
            _stop_response(
                "status: PASS\nEvidence: alternate exact entrypoint dispatched\n"
                "Gap: changed entrypoint remained quarantined"
            ),
            _stop_response(
                "status: PASS\nEvidence: corrected dispatch retained\n"
                "Gap: changed entrypoint remained unavailable"
            ),
        ]

        def boundary_error(name, args, context):
            if args.get("script_path") == changed:
                return "Authorized Skill script changed after compilation."
            return None

        with patch(
            "tools.registry.delegated_resource_boundary_error",
            side_effect=boundary_error,
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=6,
                dispatch_result=json.dumps({"status": "success"}),
                tools=[tool_name],
                schemas=schema,
                required_capability_tools=[tool_name],
                allowed_skill_scripts=[
                    ("catalog-database", "scripts/changed_helper.py", "a" * 64),
                    ("catalog-database", "scripts/alternate_helper.py", "b" * 64),
                ],
            )

        self.assertFalse(responses)
        self.assertEqual(1, dispatch_mock.await_count)
        invocation_quarantine = [
            event for event in events
            if event.get("event_type")
            == "debug.tool.invocation_quarantined"
        ]
        self.assertEqual(1, len(invocation_quarantine))
        detail = invocation_quarantine[0]["payload"]["invocations"][0]
        self.assertEqual(2, detail["distinct_iteration_count"])
        self.assertEqual([1, 2], detail["observed_iterations"])
        self.assertTrue(all("tools" in body for body in bodies[:3]))
        self.assertFalse(any(
            event.get("event_type") == "debug.tool.quarantined"
            for event in events
        ))
        terminal_calls = [
            event["payload"]
            for event in events
            if event.get("event_type") in {"tool.failed", "tool.completed"}
        ]
        self.assertEqual(
            [False, False, False, True],
            [item["actual_dispatch_attempted"] for item in terminal_calls],
        )

    async def test_unexpected_field_variants_share_preflight_fingerprint(self):
        tool_name = "run_skill_python"
        schema = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Run one exact granted Python entrypoint.",
                "parameters": {
                    "type": "object",
                    "properties": {"script_path": {"type": "string"}},
                    "required": ["script_path"],
                },
            },
        }]
        wrong = "skills/catalog-database/scripts/changed_helper.py"
        correct = "skills/catalog-database/scripts/alternate_helper.py"
        responses = [
            _tool_call_response(
                "call-extra-a",
                tool_name=tool_name,
                arguments={"script_path": wrong, "invalid_noise": "alpha"},
            ),
            _tool_call_response(
                "call-extra-b",
                tool_name=tool_name,
                arguments={"script_path": wrong, "invalid_noise": "beta"},
            ),
            _tool_call_response(
                "call-extra-corrected",
                tool_name=tool_name,
                arguments={"script_path": correct},
            ),
            _stop_response("status: PASS\nEvidence: corrected dispatch\nGap: none"),
            _stop_response(
                "status: PASS\nEvidence: corrected dispatch retained\nGap: none"
            ),
        ]

        def boundary_error(name, args, context):
            if args.get("script_path") == wrong:
                return "Authorized Skill script changed after compilation."
            return None

        with patch(
            "tools.registry.delegated_resource_boundary_error",
            side_effect=boundary_error,
        ):
            _bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=6,
                dispatch_result=json.dumps({"status": "success"}),
                tools=[tool_name],
                schemas=schema,
                required_capability_tools=[tool_name],
                allowed_skill_scripts=[(
                    "catalog-database", "scripts/alternate_helper.py", "a" * 64,
                )],
            )

        self.assertEqual(1, dispatch_mock.await_count)
        invocation_quarantine = next(
            event for event in events
            if event.get("event_type")
            == "debug.tool.invocation_quarantined"
        )
        detail = invocation_quarantine["payload"]["invocations"][0]
        self.assertEqual([1, 2], detail["observed_iterations"])
        self.assertEqual(2, detail["distinct_iteration_count"])
        self.assertFalse(any(
            event.get("event_type") == "debug.tool.quarantined"
            for event in events
        ))

    async def test_replayed_exact_preflight_quarantine_escalates_tool_without_dispatch(self):
        tool_name = "run_skill_python"
        schema = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Run one exact granted Python entrypoint.",
                "parameters": {
                    "type": "object",
                    "properties": {"script_path": {"type": "string"}},
                    "required": ["script_path"],
                },
            },
        }]
        wrong = "skills/catalog-database/scripts/invented.py"
        responses = [
            _tool_call_response(
                f"call-preflight-repeat-{index}",
                tool_name=tool_name,
                arguments={"script_path": wrong},
            )
            for index in range(3)
        ] + [
            _stop_response(
                "status: WARN/degraded\nEvidence: no handler was entered\n"
                "Gap: the requested entrypoint was outside the exact grant"
            ),
            _stop_response("must not be requested"),
        ]

        with patch(
            "tools.registry.delegated_resource_boundary_error",
            return_value=(
                "Delegated resource boundary rejected run_skill_python "
                "outside the compiled Skill/script capability closure."
            ),
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=6,
                dispatch_result=json.dumps({"status": "must-not-dispatch"}),
                tools=[tool_name],
                schemas=schema,
                required_capability_tools=[tool_name],
                allowed_skill_scripts=[(
                    "catalog-database", "scripts/catalog_helper.py", "a" * 64,
                )],
            )

        self.assertEqual(0, dispatch_mock.await_count)
        self.assertEqual(4, len(bodies))
        self.assertEqual(1, len(responses))
        self.assertTrue(all("tools" in body for body in bodies[:3]))
        self.assertNotIn("tools", bodies[3])
        exact_quarantine = [
            event for event in events
            if event.get("event_type")
            == "debug.tool.invocation_quarantined"
        ]
        self.assertEqual(1, len(exact_quarantine))
        quarantine = next(
            event for event in events
            if event.get("event_type") == "debug.tool.quarantined"
        )
        self.assertEqual(
            [tool_name], quarantine["payload"]["preflight_denial_tools"]
        )
        self.assertEqual(
            2, quarantine["payload"]["exact_preflight_denial_limit"]
        )
        detail = quarantine["payload"]["preflight_denial_details"][tool_name]
        self.assertEqual(
            "exact_quarantined_invocation_replayed",
            detail["escalation_reason"],
        )
        self.assertEqual(3, detail["exact_distinct_iteration_count"])
        self.assertFalse(detail["actual_dispatch_attempted"])
        forced = next(
            event["payload"] for event in events
            if event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get(
                "delegate_preflight_denial_synthesis"
            )
        )
        self.assertTrue(forced["delegate_forced_synthesis"])
        self.assertEqual([], forced["workflow_forced_tools"])
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_same_batch_valid_dispatch_cancels_exact_replay_tool_quarantine(self):
        tool_name = "run_skill_python"
        schema = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Run one exact granted Python entrypoint.",
                "parameters": {
                    "type": "object",
                    "properties": {"script_path": {"type": "string"}},
                    "required": ["script_path"],
                },
            },
        }]
        changed = "skills/catalog-database/scripts/changed_helper.py"
        alternate = "skills/catalog-database/scripts/alternate_helper.py"
        responses = [
            _tool_call_response(
                "call-establish-exact-1",
                tool_name=tool_name,
                arguments={"script_path": changed},
            ),
            _tool_call_response(
                "call-establish-exact-2",
                tool_name=tool_name,
                arguments={"script_path": changed},
            ),
            _tool_calls_response([
                (
                    "call-replay-quarantined",
                    tool_name,
                    {"script_path": changed},
                ),
                (
                    "call-valid-alternate",
                    tool_name,
                    {"script_path": alternate},
                ),
            ]),
            _stop_response(
                "status: PASS\nEvidence: valid alternate dispatched\nGap: none"
            ),
            _stop_response(
                "status: PASS\nEvidence: normal synthesis retained\nGap: none"
            ),
        ]

        def boundary_error(name, args, context):
            if args.get("script_path") == changed:
                return "Authorized Skill script changed after compilation."
            return None

        with patch(
            "tools.registry.delegated_resource_boundary_error",
            side_effect=boundary_error,
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=6,
                dispatch_result=json.dumps({"status": "success"}),
                tools=[tool_name],
                schemas=schema,
                required_capability_tools=[tool_name],
                allowed_skill_scripts=[
                    ("catalog-database", "scripts/changed_helper.py", "a" * 64),
                    ("catalog-database", "scripts/alternate_helper.py", "b" * 64),
                ],
            )

        self.assertFalse(responses)
        self.assertEqual(1, dispatch_mock.await_count)
        self.assertTrue(all("tools" in body for body in bodies[:4]))
        next_request_messages = bodies[3]["messages"]
        batch_index = next(
            index
            for index, message in enumerate(next_request_messages)
            if message.get("role") == "assistant"
            and {
                call.get("id")
                for call in message.get("tool_calls") or []
            } == {
                "call-replay-quarantined",
                "call-valid-alternate",
            }
        )
        batch_results = next_request_messages[
            batch_index + 1:batch_index + 3
        ]
        self.assertEqual(["tool", "tool"], [
            message.get("role") for message in batch_results
        ])
        self.assertEqual(
            {
                "call-replay-quarantined",
                "call-valid-alternate",
            },
            {
                message.get("tool_call_id") for message in batch_results
            },
        )
        self.assertFalse(any(
            event.get("event_type") == "debug.tool.quarantined"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get(
                "delegate_preflight_denial_synthesis"
            )
            for event in events
        ))
        terminal_calls = [
            event["payload"]
            for event in events
            if event.get("event_type") in {"tool.failed", "tool.completed"}
        ]
        self.assertEqual(
            [False, False, False, True],
            [item["actual_dispatch_attempted"] for item in terminal_calls],
        )
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_three_distinct_cross_iteration_preflight_denials_close_tool(self):
        tool_name = "run_skill_python"
        schema = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Run one exact granted Python entrypoint.",
                "parameters": {
                    "type": "object",
                    "properties": {"script_path": {"type": "string"}},
                    "required": ["script_path"],
                },
            },
        }]
        wrong_paths = [
            f"skills/catalog-database/scripts/invented_{index}.py"
            for index in range(3)
        ]
        responses = [
            _tool_call_response(
                f"call-distinct-{index}",
                tool_name=tool_name,
                arguments={"script_path": path},
            )
            for index, path in enumerate(wrong_paths)
        ] + [
            _stop_response(
                "status: WARN/degraded\nEvidence: no handler entered\n"
                "Gap: three distinct invalid entrypoints were rejected"
            ),
        ]

        with patch(
            "tools.registry.delegated_resource_boundary_error",
            return_value="Outside exact compiled script capability closure.",
        ):
            bodies, dispatch_mock, events, _persisted = await self._run(
                responses,
                max_iterations=6,
                dispatch_result=json.dumps({"status": "must-not-dispatch"}),
                tools=[tool_name],
                schemas=schema,
                required_capability_tools=[tool_name],
                allowed_skill_scripts=[(
                    "catalog-database", "scripts/real.py", "a" * 64,
                )],
            )

        self.assertFalse(responses)
        self.assertEqual(0, dispatch_mock.await_count)
        self.assertEqual(4, len(bodies))
        self.assertNotIn("tools", bodies[3])
        quarantine = next(
            event for event in events
            if event.get("event_type") == "debug.tool.quarantined"
        )
        detail = quarantine["payload"]["preflight_denial_details"][tool_name]
        self.assertEqual(
            "distinct_semantic_preflight_denial_limit",
            detail["escalation_reason"],
        )
        self.assertEqual(3, detail["distinct_semantic_denial_count"])
        self.assertEqual(
            3, quarantine["payload"]["distinct_preflight_denial_limit"]
        )

    async def test_identical_success_without_progress_is_quarantined(self):
        responses = [
            _tool_call_response(
                "call-success-1",
                arguments={"query": "bounded evidence alpha"},
            ),
            _tool_call_response(
                "call-success-2",
                arguments={"query": "bounded evidence beta"},
            ),
            _tool_call_response(
                "call-success-3",
                arguments={"query": "bounded evidence gamma"},
            ),
            _stop_response(
                "status: WARN/degraded\nEvidence: repeated empty result\n"
                "Gap: no distinct evidence was returned"
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result=json.dumps({"status": "ok", "results": []}),
        )

        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 3)
        self.assertTrue(all("tools" in body for body in request_bodies[:3]))
        self.assertNotIn("tools", request_bodies[3])
        observations = [
            event for event in events
            if event.get("event_type") == "debug.tool.no_progress_success"
        ]
        self.assertEqual(
            [event["payload"]["identical_success_count"] for event in observations],
            [2, 3],
        )
        self.assertEqual(observations[-1]["payload"]["argument_variants"], 3)
        quarantine_debug = [
            event for event in events
            if event.get("event_type") == "debug.tool.quarantined"
        ]
        self.assertEqual(len(quarantine_debug), 1)
        self.assertEqual(
            quarantine_debug[0]["payload"]["no_progress_success_tools"],
            ["web_search"],
        )
        self.assertEqual(quarantine_debug[0]["payload"]["failure_tools"], [])
        self.assertEqual(
            quarantine_debug[0]["payload"]["no_progress_success_limit"],
            3,
        )
        final_messages = request_bodies[3]["messages"]
        self.assertTrue(any(
            message.get("role") == "user"
            and "successful results without artifact/workflow progress"
            in str(message.get("content"))
            for message in final_messages
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_quarantined_capability_can_still_close_visible_length_once(self):
        partial = (
            "status: WARN/degraded\n"
            "Evidence: three identical bounded search results were retained.\n"
            "Gap: no distinct evidence was returned before truncation"
        )
        completion = (
            "Conclusion: report the repeated-result limitation without retrying.\n"
            + _result_fields_footer("evidence_status", degraded=True)
        )
        responses = [
            _tool_call_response(
                "call-quarantine-length-1",
                arguments={"query": "bounded evidence alpha"},
            ),
            _tool_call_response(
                "call-quarantine-length-2",
                arguments={"query": "bounded evidence beta"},
            ),
            _tool_call_response(
                "call-quarantine-length-3",
                arguments={"query": "bounded evidence gamma"},
            ),
            _visible_length_response(partial),
            _stop_response(completion),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=7,
            dispatch_result=json.dumps({"status": "ok", "results": []}),
            required_result_fields=["evidence_status"],
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 5)
        self.assertEqual(dispatch_mock.await_count, 3)
        self.assertTrue(all("tools" in body for body in request_bodies[:3]))
        self.assertNotIn("tools", request_bodies[3])
        self.assertNotIn("tools", request_bodies[4])
        self.assertEqual(sum(
            event.get("event_type") == "debug.tool.quarantined"
            for event in events
        ), 1)
        recovery = next(
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
        )
        self.assertEqual(recovery["payload"]["dispatched_tool_result_count"], 3)
        origin_iteration = next(
            event["payload"] for event in events
            if event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get("iteration") == 4
        )
        self.assertFalse(origin_iteration["delegate_forced_synthesis"])
        emitted = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertIn(partial + "\n" + completion, emitted)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_artifact_progress_breaks_identical_success_streak(self):
        # This test isolates convergence accounting.  Use the ordinary
        # artifact writer rather than a managed Skill runner,
        # whose schema is now correctly hidden without a compiler-issued,
        # content-addressed script grant.
        tool_name = "write_file"
        schema = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Run a declared script.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["filepath", "content"],
                },
            },
        }]
        responses = [
            _tool_call_response(
                f"call-artifact-{index}",
                tool_name=tool_name,
                arguments={
                    "filepath": f"artifact-{index}.json",
                    "content": "{}",
                },
            )
            for index in range(3)
        ] + [
            _stop_response("status: PASS\nEvidence: artifacts created\nGap: none"),
        ]
        identical_result = json.dumps({
            "status": "written",
            "path": "evidence.json",
            "size": 42,
        })

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result=identical_result,
            tools=[tool_name],
            schemas=schema,
        )

        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 3)
        self.assertFalse(any(
            event.get("event_type") in {
                "debug.tool.no_progress_success",
                "debug.tool.quarantined",
            }
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "verifier.requested"
            for event in events
        ))
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_interleaved_tool_does_not_hide_repeated_semantic_success(self):
        extract_schema = {
            "type": "function",
            "function": {
                "name": "web_extract",
                "description": "Extract a bounded source.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        }
        responses = [
            _tool_call_response("call-streak-1"),
            _tool_call_response("call-streak-2"),
            _tool_call_response(
                "call-interrupt",
                tool_name="web_extract",
                arguments={"url": "https://evidence.invalid/source"},
            ),
            _tool_call_response("call-streak-3"),
            _stop_response(
                "status: PASS\nEvidence: bounded results reused\nGap: none"
            ),
        ]

        _bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result=json.dumps({"status": "ok", "results": []}),
            tools=["web_search", "web_extract"],
            schemas=self.web_schema + [extract_schema],
        )

        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 4)
        observations = [
            event for event in events
            if event.get("event_type") == "debug.tool.no_progress_success"
        ]
        self.assertEqual(
            [event["payload"]["identical_success_count"] for event in observations],
            [2, 3],
        )
        self.assertTrue(any(
            event.get("event_type") == "debug.tool.quarantined"
            for event in events
        ))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_volatile_executor_receipts_do_not_hide_semantic_duplicate(self):
        responses = [
            _tool_call_response(f"volatile-{index}", arguments={"query": "same"})
            for index in range(3)
        ] + [
            _stop_response(
                "status: WARN/degraded\nEvidence: duplicate demo payload\nGap: no task evidence"
            )
        ]
        dispatch_results = [
            json.dumps({
                "status": "success",
                "request_id": f"request-{index}",
                "duration_seconds": 0.1 * index,
                "stdout": "same demo payload",
                "result": None,
                "artifacts": [],
            })
            for index in range(3)
        ]

        bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result="",
            dispatch_results=dispatch_results,
        )

        self.assertEqual(3, dispatch_mock.await_count)
        self.assertNotIn("tools", bodies[3])
        observations = [
            event["payload"] for event in events
            if event.get("event_type") == "debug.tool.no_progress_success"
        ]
        self.assertEqual(
            [2, 3],
            [item["identical_success_count"] for item in observations],
        )
        self.assertTrue(observations[-1]["same_invocation_repeated"])

    async def test_prerequisite_reader_is_excluded_from_success_quarantine(self):
        tool_name = "read_file"
        schema = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Read a prerequisite.",
                "parameters": {
                    "type": "object",
                    "properties": {"filepath": {"type": "string"}},
                    "required": ["filepath"],
                },
            },
        }]
        responses = [
            _tool_call_response(
                f"call-reader-{index}",
                tool_name=tool_name,
                arguments={"filepath": f"input-{index}.md"},
            )
            for index in range(3)
        ] + [_stop_response("status: PASS\nEvidence: prerequisites read\nGap: none")]

        _bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=5,
            dispatch_result=json.dumps({"status": "ok", "content": "same"}),
            tools=[tool_name],
            schemas=schema,
        )

        self.assertFalse(responses)
        self.assertEqual(dispatch_mock.await_count, 3)
        self.assertFalse(any(
            event.get("event_type") in {
                "debug.tool.no_progress_success",
                "debug.tool.quarantined",
            }
            for event in events
        ))

    async def test_reasoning_only_length_gets_one_in_budget_recovery(self):
        responses = [
            _reasoning_length_response(),
            _tool_call_response(),
            _stop_response(
                "status: PASS\nEvidence: bounded search result\nGap: none"
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=3,
            dispatch_result=json.dumps({
                "status": "ok",
                "results": [{"title": "bounded evidence"}],
            }),
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 3)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertIn("tools", request_bodies[0])
        self.assertIn("tools", request_bodies[1])
        self.assertNotIn("tools", request_bodies[2])
        recovery_messages = request_bodies[1]["messages"]
        self.assertTrue(any(
            message.get("role") == "user"
            and "single bounded recovery turn" in str(message.get("content"))
            for message in recovery_messages
        ))
        self.assertFalse(any(
            "internal-chain" in str(message.get("content"))
            for message in recovery_messages
        ))
        recovery_events = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_reasoning_only_length_recovery"
        ]
        self.assertEqual(len(recovery_events), 1)
        self.assertEqual(recovery_events[0]["payload"]["recovery_count"], 1)
        self.assertEqual(recovery_events[0]["payload"]["max_recoveries"], 1)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_second_reasoning_only_length_is_terminal(self):
        responses = [
            _reasoning_length_response("first-private-reasoning " * 200),
            _reasoning_length_response("second-private-reasoning " * 200),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({"status": "ok"}),
        )

        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        recovery_events = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_reasoning_only_length_recovery"
        ]
        self.assertEqual(len(recovery_events), 1)
        terminal = [
            event for event in events
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["payload"]["finish_reason"], "length")
        self.assertEqual(terminal[0]["payload"]["partial_content_chars"], 0)
        self.assertEqual(events[-1]["type"], "error")

    async def test_visible_partial_length_is_not_reasoning_only_recovered(self):
        responses = [
            _reasoning_length_response(
                "private-reasoning " * 100,
                content="status: WARN\nEvidence: partial child payload",
            ),
            _stop_response("must not be requested"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=4,
            dispatch_result=json.dumps({"status": "ok"}),
        )

        self.assertEqual(len(request_bodies), 1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_reasoning_only_length_recovery"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ))
        self.assertEqual(events[-1]["type"], "error")

    async def test_visible_length_after_reasoning_recovery_closes_once(self):
        responses = [
            _reasoning_length_response(),
            _tool_call_response("call-after-reasoning-recovery"),
            _visible_length_response(
                "status: WARN\nEvidence: retained result after dispatch"
            ),
            _stop_response(
                "Conclusion: retain WARN with explicit bounded provenance."
            ),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=6,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
        )

        self.assertEqual(len(request_bodies), 4)
        self.assertEqual(len(responses), 0)
        self.assertEqual(dispatch_mock.await_count, 1)
        self.assertEqual(sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_reasoning_only_length_recovery"
            for event in events
        ), 1)
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ))
        self.assertNotIn("tools", request_bodies[-1])
        self.assertEqual(events[-1]["type"], "done")

    async def test_primary_visible_length_never_uses_delegate_recovery(self):
        responses = [
            _visible_length_response("A truncated primary answer"),
            _stop_response("must not be requested because budget is exhausted"),
        ]

        request_bodies, dispatch_mock, events, _persisted = await self._run(
            responses,
            max_iterations=1,
            dispatch_result=json.dumps({"status": "ok", "results": [1]}),
            delegated=False,
        )

        self.assertEqual(len(request_bodies), 1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(dispatch_mock.await_count, 0)
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ))
        self.assertEqual(events[-1]["type"], "error")


if __name__ == "__main__":
    unittest.main()
