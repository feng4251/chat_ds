import asyncio
import httpx
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

from agent_loop import (
    DirectToolExposure,
    _iter_provider_stream,
    _safe_parse_args,
    run_stream,
)
from tool_call_stream import (
    ToolCallStreamAccumulator,
    validate_nonstream_tool_call_batch,
)


def _fragment(
    *,
    call_id=None,
    index=None,
    include_index=True,
    name=None,
    arguments=None,
):
    fragment = {"id": call_id, "function": {}}
    if include_index:
        fragment["index"] = index
    if name is not None:
        fragment["function"]["name"] = name
    if arguments is not None:
        fragment["function"]["arguments"] = arguments
    return fragment


class ToolCallStreamAccumulatorTests(unittest.TestCase):
    def test_single_delta(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        accumulator.add_fragment(_fragment(
            call_id="provider-call",
            index=0,
            name="read_file",
            arguments='{"filepath":"report.md"}',
        ))

        assembly = accumulator.finalize(iteration=3)

        self.assertTrue(assembly.ok)
        self.assertEqual(1, len(assembly.calls))
        self.assertEqual("provider-call", assembly.calls[0].call_id)
        self.assertEqual("read_file", assembly.calls[0].name)
        self.assertEqual(
            {"filepath": "report.md"},
            json.loads(assembly.calls[0].arguments),
        )

    def test_parallel_calls_interleave_using_index_for_idless_continuations(self):
        accumulator = ToolCallStreamAccumulator({"read_file", "web_search"})
        accumulator.add_fragment(_fragment(
            call_id="read-id", index=0, name="read_file", arguments='{"file',
        ))
        accumulator.add_fragment(_fragment(
            call_id="search-id", index=1, name="web_search", arguments='{"query',
        ))
        accumulator.add_fragment(_fragment(
            index=0, name=None, arguments='path":"a.md"}',
        ))
        accumulator.add_fragment(_fragment(
            index=1, name=None, arguments='":"term"}',
        ))

        assembly = accumulator.finalize(iteration=4)

        self.assertTrue(assembly.ok)
        self.assertEqual(
            ["read_file", "web_search"],
            [call.name for call in assembly.calls],
        )
        self.assertEqual({"filepath": "a.md"}, json.loads(assembly.calls[0].arguments))
        self.assertEqual({"query": "term"}, json.loads(assembly.calls[1].arguments))
        self.assertEqual(2, assembly.debug["index_continuation_count"])

    def test_reused_index_with_new_id_creates_a_new_logical_call(self):
        accumulator = ToolCallStreamAccumulator({"read_file", "web_search"})
        accumulator.add_fragment(_fragment(
            call_id="first", index=0, name="read_file", arguments='{"filepath":"a"}',
        ))
        accumulator.add_fragment(_fragment(
            call_id="second", index=0, name="web_search", arguments='{"query":"b"}',
        ))

        assembly = accumulator.finalize(iteration=5)

        self.assertTrue(assembly.ok)
        self.assertEqual(2, len(assembly.calls))
        self.assertEqual({"filepath": "a"}, json.loads(assembly.calls[0].arguments))
        self.assertEqual({"query": "b"}, json.loads(assembly.calls[1].arguments))
        self.assertEqual(1, assembly.debug["provider_index_reuse_count"])

    def test_missing_indexes_with_different_ids_remain_separate(self):
        accumulator = ToolCallStreamAccumulator({"read_file", "web_search"})
        accumulator.add_fragment(_fragment(
            call_id="first",
            include_index=False,
            name="read_file",
            arguments='{"filepath":"a"}',
        ))
        accumulator.add_fragment(_fragment(
            call_id="second",
            include_index=False,
            name="web_search",
            arguments='{"query":"b"}',
        ))

        assembly = accumulator.finalize(iteration=6)

        self.assertTrue(assembly.ok)
        self.assertEqual(2, len(assembly.calls))
        self.assertEqual(2, assembly.debug["missing_index_fragment_count"])

    def test_split_name_is_rebuilt_only_when_it_uniquely_matches_exposed_tool(self):
        accumulator = ToolCallStreamAccumulator({"read_file", "web_search"})
        accumulator.add_fragment(_fragment(
            call_id="split", index=0, name="read_", arguments='{"filepath":"a"}',
        ))
        accumulator.add_fragment(_fragment(
            call_id="split", index=0, name="file",
        ))

        assembly = accumulator.finalize(iteration=7)

        self.assertTrue(assembly.ok)
        self.assertEqual("read_file", assembly.calls[0].name)

    def test_valid_name_cannot_be_overwritten_by_conflicting_short_fragment(self):
        accumulator = ToolCallStreamAccumulator({"read_file", "web_search"})
        accumulator.add_fragment(_fragment(
            call_id="conflict", index=0, name="read_file", arguments='{}',
        ))
        accumulator.add_fragment(_fragment(
            call_id="conflict", index=0, name="r",
        ))

        assembly = accumulator.finalize(iteration=8)

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.calls)
        self.assertIn("tool_name_conflict", assembly.errors)

    def test_rejected_name_debug_records_shape_without_name_value(self):
        accumulator = ToolCallStreamAccumulator(
            {"read_file", "web_search"}
        )
        accumulator.add_fragment(_fragment(
            call_id="foreign",
            index=0,
            name="invented_browser_action",
            arguments="{}",
        ))

        assembly = accumulator.finalize(iteration=8)

        self.assertFalse(assembly.ok)
        call_debug = assembly.debug["calls"][0]
        self.assertEqual(
            "foreign_or_conflict",
            call_debug["name_resolution"],
        )
        self.assertEqual(
            1,
            call_debug["name_relation_counts"]["foreign"],
        )
        self.assertEqual(
            len("invented_browser_action"),
            call_debug["name_fragment_chars_total"],
        )
        rendered = json.dumps(assembly.debug, ensure_ascii=False)
        self.assertNotIn("invented_browser_action", rendered)

    def test_split_exposed_name_debug_resolves_exactly(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        accumulator.add_fragment(_fragment(
            call_id="split",
            index=0,
            name="read_",
            arguments="{}",
        ))
        accumulator.add_fragment(_fragment(
            call_id="split",
            index=0,
            name="file",
        ))

        assembly = accumulator.finalize(iteration=8)

        self.assertTrue(assembly.ok)
        call_debug = assembly.debug["calls"][0]
        self.assertEqual("exact_exposed", call_debug["name_resolution"])
        self.assertEqual(2, call_debug["name_fragment_count"])

    def test_complete_call_is_retained_for_review_when_peer_is_malformed(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        accumulator.add_fragment(_fragment(
            call_id="complete",
            index=0,
            name="read_file",
            arguments='{"filepath":"evidence.md"}',
        ))
        accumulator.add_fragment(_fragment(
            call_id="malformed",
            index=1,
            name="read_file",
            arguments='{"filepath":',
        ))

        assembly = accumulator.finalize(iteration=8)

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.calls)
        self.assertEqual(1, len(assembly.complete_calls))
        self.assertEqual("read_file", assembly.complete_calls[0].name)
        self.assertEqual(
            {"filepath": "evidence.md"},
            json.loads(assembly.complete_calls[0].arguments),
        )
        self.assertEqual(1, assembly.debug["complete_call_count"])
        encoded_debug = json.dumps(assembly.debug, ensure_ascii=False)
        self.assertNotIn("evidence.md", encoded_debug)
        self.assertNotIn('"complete"', encoded_debug)

    def test_reused_provider_id_with_conflicting_arguments_is_not_complete(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        for index, filepath in enumerate(("first.md", "second.md")):
            accumulator.add_fragment(_fragment(
                call_id="reused-provider-id",
                index=index,
                name="read_file" if index == 0 else None,
                arguments=json.dumps({"filepath": filepath}),
            ))

        assembly = accumulator.finalize(iteration=8)

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.calls)
        self.assertEqual((), assembly.complete_calls)
        self.assertIn("ambiguous_arguments_json", assembly.errors)

    def test_batch_limit_error_disables_all_complete_call_review(self):
        with patch(
            "tool_call_stream._MAX_ARGUMENT_CHARS_PER_BATCH",
            20,
        ):
            accumulator = ToolCallStreamAccumulator({"read_file"})
            accumulator.add_fragment(_fragment(
                call_id="first",
                index=0,
                name="read_file",
                arguments='{"filepath":"first.md"}',
            ))
            accumulator.add_fragment(_fragment(
                call_id="second",
                index=1,
                name="read_file",
                arguments='{"filepath":"second.md"}',
            ))

            assembly = accumulator.finalize(iteration=8)

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.complete_calls)
        self.assertIn("tool_argument_batch_limit_exceeded", assembly.errors)

    def test_missing_streamed_name_is_not_inferred_from_single_exposed_tool(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        accumulator.add_fragment(_fragment(
            call_id="missing-name",
            index=0,
            arguments='{"filepath":"a.md"}',
        ))

        assembly = accumulator.finalize(iteration=8)

        self.assertFalse(assembly.ok)
        self.assertIn("missing_tool_name", assembly.errors)

    def test_incomplete_name_prefix_is_never_completed_at_end_of_stream(self):
        for exposed_name, prefix in (
            ("write_file", "w"),
            ("merge_files", "m"),
            ("read_file", "r"),
        ):
            with self.subTest(exposed_name=exposed_name, prefix=prefix):
                accumulator = ToolCallStreamAccumulator({exposed_name})
                accumulator.add_fragment(_fragment(
                    call_id="truncated-name",
                    index=0,
                    name=prefix,
                    arguments="{}",
                ))

                assembly = accumulator.finalize(iteration=8)

                self.assertFalse(assembly.ok)
                self.assertEqual((), assembly.calls)
                self.assertIn("tool_name_incomplete", assembly.errors)

    def test_streamed_non_function_call_type_rejects_batch(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        fragment = _fragment(
            call_id="wrong-type",
            index=0,
            name="read_file",
            arguments="{}",
        )
        fragment["type"] = "custom"
        accumulator.add_fragment(fragment)

        assembly = accumulator.finalize(iteration=8)

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.calls)
        self.assertIn("tool_call_type_invalid", assembly.errors)

    def test_nonstream_fallback_rejects_duplicate_ids_atomically(self):
        payload = {
            "choices": [{
                "message": {
                    "tool_calls": [
                        {
                            "id": "duplicate-secret-id",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":"a.md"}',
                            },
                        },
                        {
                            "id": "duplicate-secret-id",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":"b.md"}',
                            },
                        },
                    ],
                },
            }],
        }

        assembly = validate_nonstream_tool_call_batch(payload, {"read_file"})

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.calls)
        self.assertIn("fallback_duplicate_tool_call_id", assembly.errors)
        encoded = json.dumps(assembly.debug, ensure_ascii=False)
        self.assertNotIn("duplicate-secret-id", encoded)
        self.assertNotIn("filepath", encoded)

    def test_nonstream_complete_call_is_retained_when_peer_is_malformed(self):
        payload = {
            "choices": [{
                "message": {
                    "tool_calls": [
                        {
                            "id": "complete-fallback",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":"evidence.md"}',
                            },
                        },
                        {
                            "id": "malformed-fallback",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":',
                            },
                        },
                    ],
                },
            }],
        }

        assembly = validate_nonstream_tool_call_batch(payload, {"read_file"})

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.calls)
        self.assertEqual(1, len(assembly.complete_calls))
        self.assertEqual(
            {"filepath": "evidence.md"},
            json.loads(assembly.complete_calls[0].arguments),
        )
        self.assertEqual(1, assembly.debug["complete_call_count"])
        encoded = json.dumps(assembly.debug, ensure_ascii=False)
        self.assertNotIn("evidence.md", encoded)
        self.assertNotIn("complete-fallback", encoded)

    def test_nonstream_foreign_name_debug_is_payload_free(self):
        foreign_name = "invented_browser_action"
        payload = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "foreign-call",
                        "type": "function",
                        "function": {
                            "name": foreign_name,
                            "arguments": "{}",
                        },
                    }],
                },
            }],
        }

        assembly = validate_nonstream_tool_call_batch(
            payload,
            {"browser_navigate"},
        )

        self.assertFalse(assembly.ok)
        self.assertEqual(
            "foreign",
            assembly.debug["calls"][0]["name_relation"],
        )
        self.assertEqual(
            len(foreign_name),
            assembly.debug["calls"][0]["name_chars"],
        )
        self.assertNotIn(
            foreign_name,
            json.dumps(assembly.debug, ensure_ascii=False),
        )

    def test_cumulative_argument_snapshot_replaces_instead_of_duplicates(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        accumulator.add_fragment(_fragment(
            call_id="snapshot", index=0, name="read_file", arguments='{"filepath":',
        ))
        accumulator.add_fragment(_fragment(
            call_id="snapshot", index=0, arguments='{"filepath":"a.md"}',
        ))
        accumulator.add_fragment(_fragment(
            call_id="snapshot", index=0, arguments='{"filepath":"a.md"}',
        ))

        assembly = accumulator.finalize(iteration=9)

        self.assertTrue(assembly.ok)
        self.assertEqual({"filepath": "a.md"}, json.loads(assembly.calls[0].arguments))
        self.assertEqual(2, assembly.debug["cumulative_argument_snapshot_count"])

    def test_hybrid_delta_then_full_snapshot_reset_has_one_valid_object(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        accumulator.add_fragment(_fragment(
            call_id="hybrid", index=0, name="read_file", arguments='{"file',
        ))
        accumulator.add_fragment(_fragment(
            call_id="hybrid", index=0, arguments='path":"stale.md",',
        ))
        # This provider fragment is a cumulative full snapshot, even though it
        # is not a prefix-extension of the preceding character deltas.
        accumulator.add_fragment(_fragment(
            call_id="hybrid", index=0, arguments='{"filepath":"report',
        ))
        accumulator.add_fragment(_fragment(
            call_id="hybrid", index=0, arguments='.md"}',
        ))

        assembly = accumulator.finalize(iteration=9)

        self.assertTrue(assembly.ok)
        self.assertEqual(
            {"filepath": "report.md"},
            json.loads(assembly.calls[0].arguments),
        )
        argument_debug = assembly.debug["calls"][0]["argument_json"]
        self.assertEqual(1, argument_debug["valid_semantic_object_count"])
        self.assertEqual(
            "snapshot_compatibility",
            argument_debug["selected_argument_mode"],
        )
        self.assertTrue(argument_debug["compatibility_activated"])
        encoded_debug = json.dumps(assembly.debug, ensure_ascii=False)
        self.assertNotIn("stale.md", encoded_debug)
        self.assertNotIn("report.md", encoded_debug)

    def test_conflicting_complete_root_reset_is_ambiguous_and_atomic(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        accumulator.add_fragment(_fragment(
            call_id="conflicting-reset",
            index=0,
            name="read_file",
            arguments='{"filepath":"first.md"}',
        ))
        accumulator.add_fragment(_fragment(
            call_id="conflicting-reset",
            index=0,
            arguments='{"filepath":"second.md"}',
        ))

        assembly = accumulator.finalize(iteration=9)

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.calls)
        self.assertIn("ambiguous_arguments_json", assembly.errors)
        call_debug = assembly.debug["calls"][0]
        self.assertEqual(1, call_debug["protected_argument_candidate_count"])
        self.assertEqual(
            2,
            call_debug["argument_json"]["valid_semantic_object_count"],
        )
        encoded_debug = json.dumps(assembly.debug, ensure_ascii=False)
        self.assertNotIn("first.md", encoded_debug)
        self.assertNotIn("second.md", encoded_debug)

    def test_distinct_valid_reconstructions_are_ambiguous_and_atomic(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        # Append and full-snapshot interpretations both remain structurally
        # possible, then close into two different valid JSON objects.
        fragments = ('{"x":"', '1}', '{"x":"', '"}')
        accumulator.add_fragment(_fragment(
            call_id="ambiguous",
            index=0,
            name="read_file",
            arguments=fragments[0],
        ))
        for fragment in fragments[1:]:
            accumulator.add_fragment(_fragment(
                call_id="ambiguous", index=0, arguments=fragment,
            ))

        assembly = accumulator.finalize(iteration=10)

        self.assertFalse(assembly.ok)
        # Atomic empty output is what prevents any downstream dispatch.
        self.assertEqual((), assembly.calls)
        self.assertIn("ambiguous_arguments_json", assembly.errors)
        argument_debug = assembly.debug["calls"][0]["argument_json"]
        self.assertEqual(2, argument_debug["valid_semantic_object_count"])
        encoded_debug = json.dumps(assembly.debug, ensure_ascii=False)
        self.assertNotIn('"x"', encoded_debug)

    def test_cross_call_candidate_state_budget_is_hard_and_atomic(self):
        with patch(
            "tool_call_stream."
            "_MAX_ARGUMENT_RECONSTRUCTION_STATE_CHARS_PER_BATCH",
            24,
        ):
            accumulator = ToolCallStreamAccumulator({"read_file", "web_search"})
            accumulator.add_fragment(_fragment(
                call_id="first",
                index=0,
                name="read_file",
                arguments='{"value":"123"}',
            ))
            accumulator.add_fragment(_fragment(
                call_id="second",
                index=1,
                name="web_search",
                arguments='{"value":"456"}',
            ))

            assembly = accumulator.finalize(iteration=10)

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.calls)
        self.assertIn("tool_argument_batch_limit_exceeded", assembly.errors)
        self.assertLessEqual(
            assembly.debug["argument_candidate_state_chars_total"],
            24,
        )

    def test_equivalent_json_candidates_are_one_semantic_object(self):
        accumulator = ToolCallStreamAccumulator({"read_file"})
        accumulator.add_fragment(_fragment(
            call_id="equivalent",
            index=0,
            name="read_file",
            arguments=" ",
        ))
        accumulator.add_fragment(_fragment(
            call_id="equivalent", index=0, arguments='{"x":1}',
        ))
        # The append and snapshot candidates differ only in insignificant
        # whitespace and therefore resolve to one semantic object.
        assembly = accumulator.finalize(iteration=10)

        self.assertTrue(assembly.ok)
        self.assertEqual({"x": 1}, json.loads(assembly.calls[0].arguments))
        self.assertEqual(
            1,
            assembly.debug["calls"][0]["argument_json"][
                "valid_object_candidate_count"
            ],
        )
        self.assertEqual(
            1,
            assembly.debug["calls"][0]["argument_json"][
                "valid_semantic_object_count"
            ],
        )
        self.assertEqual(
            "standard_delta",
            assembly.debug["calls"][0]["argument_json"][
                "selected_argument_mode"
            ],
        )

    def test_long_character_delta_exceeding_legacy_fragment_cap_round_trips(self):
        raw = json.dumps(
            {"filepath": "large.md", "content": "x" * 12_000},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        accumulator = ToolCallStreamAccumulator({"write_file"})
        for index, character in enumerate(raw):
            accumulator.add_fragment(_fragment(
                call_id="long-delta" if index == 0 else None,
                index=0,
                name="write_file" if index == 0 else None,
                arguments=character,
            ))

        assembly = accumulator.finalize(iteration=10)

        self.assertGreater(accumulator.fragment_count, 8_192)
        self.assertTrue(assembly.ok, assembly.errors)
        self.assertEqual(json.loads(raw), json.loads(assembly.calls[0].arguments))
        self.assertEqual(
            "standard_delta",
            assembly.debug["calls"][0]["argument_json"][
                "selected_argument_mode"
            ],
        )

    def test_braces_at_string_chunk_boundaries_do_not_open_snapshot_paths(self):
        brace_chunks = [f"{{code_{index}}}" for index in range(64)]
        fragments = [
            '{"filepath":"code.md","content":"prefix ',
            *brace_chunks,
            ' suffix"}',
        ]
        raw = "".join(fragments)
        accumulator = ToolCallStreamAccumulator({"write_file"})
        for index, fragment in enumerate(fragments):
            accumulator.add_fragment(_fragment(
                call_id="brace-delta" if index == 0 else None,
                index=0,
                name="write_file" if index == 0 else None,
                arguments=fragment,
            ))

        assembly = accumulator.finalize(iteration=10)

        self.assertTrue(assembly.ok, assembly.errors)
        self.assertEqual(json.loads(raw), json.loads(assembly.calls[0].arguments))
        call_debug = assembly.debug["calls"][0]
        self.assertFalse(call_debug["compatibility_activated"])
        self.assertEqual(
            "standard_delta",
            call_debug["argument_json"]["selected_argument_mode"],
        )

    def test_malformed_and_non_object_json_reject_entire_batch(self):
        for raw, expected_error in (
            ('{"filepath":', "malformed_arguments_json"),
            ('["a.md"]', "arguments_not_object"),
        ):
            with self.subTest(raw=raw):
                accumulator = ToolCallStreamAccumulator({"read_file"})
                accumulator.add_fragment(_fragment(
                    call_id="bad", index=0, name="read_file", arguments=raw,
                ))
                assembly = accumulator.finalize(iteration=10)
                self.assertFalse(assembly.ok)
                self.assertEqual((), assembly.calls)
                self.assertIn(expected_error, assembly.errors)
                # Structural diagnostics must not expose payload content or ids.
                encoded_debug = json.dumps(assembly.debug, ensure_ascii=False)
                self.assertNotIn("filepath", encoded_debug)
                self.assertNotIn("bad", encoded_debug)

    def test_excessively_nested_json_fails_closed_without_recursion_error(self):
        raw = '{"value":' + ("[" * 1_100) + "0" + ("]" * 1_100) + "}"
        accumulator = ToolCallStreamAccumulator({"read_file"})
        accumulator.add_fragment(_fragment(
            call_id="deep", index=0, name="read_file", arguments=raw,
        ))

        assembly = accumulator.finalize(iteration=11)
        parsed = _safe_parse_args(raw)

        self.assertFalse(assembly.ok)
        self.assertEqual((), assembly.calls)
        self.assertIn("arguments_nesting_limit_exceeded", assembly.errors)
        self.assertIn("nesting limit", parsed["__tool_arg_parse_error"])
        self.assertNotIn("value", json.dumps(assembly.debug, ensure_ascii=False))


class _FakeLineResponse:
    status_code = 200

    def __init__(self, lines, *, auto_frame=True):
        self._lines = lines
        self._auto_frame = auto_frame

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            if isinstance(line, BaseException):
                raise line
            yield line
            # Most historical fixtures list one JSON event per line.  Emit
            # the protocol-required blank delimiter so production parsing can
            # remain strict; raw framing tests opt out explicitly.
            if (
                self._auto_frame
                and isinstance(line, str)
                and line.startswith("data")
            ):
                yield ""


class _FakeHTTPErrorLineResponse(_FakeLineResponse):
    def __init__(self, status_code, body):
        super().__init__([])
        self.status_code = status_code
        self._body = body.encode("utf-8")

    async def aread(self):
        return self._body


class _FakeJSONResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class ProviderStreamUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_only_empty_choices_chunk_is_preserved(self):
        response = _FakeLineResponse([
            "data: " + json.dumps({
                "choices": [{
                    "delta": {},
                    "finish_reason": "stop",
                }],
            }),
            "data: " + json.dumps({
                "choices": [],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            }),
            "data: [DONE]",
        ])

        events = [event async for event in _iter_provider_stream(response, "openai")]

        self.assertEqual(2, len(events))
        self.assertEqual(18, events[1]["usage"]["total_tokens"])

    async def test_data_field_without_optional_space_is_accepted(self):
        payload = {
            "choices": [{
                "delta": {"content": "hello"},
                "finish_reason": "stop",
            }],
        }
        response = _FakeLineResponse(
            ["data:" + json.dumps(payload), "", "data:[DONE]", ""],
            auto_frame=False,
        )

        events = [event async for event in _iter_provider_stream(response, "openai")]

        self.assertEqual("hello", events[0]["content"])
        self.assertEqual("stop", events[0]["finish_reason"])

    async def test_leading_bom_is_ignored_once(self):
        payload = {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
        }
        response = _FakeLineResponse(
            [
                "\ufeffdata: " + json.dumps(payload),
                "",
                "data: [DONE]",
                "",
            ],
            auto_frame=False,
        )

        events = [event async for event in _iter_provider_stream(response, "openai")]

        self.assertEqual("stop", events[0]["finish_reason"])

    async def test_data_field_without_colon_is_an_empty_malformed_event(self):
        response = _FakeLineResponse(["data", ""], auto_frame=False)

        with self.assertRaises(httpx.RemoteProtocolError):
            _ = [
                event
                async for event in _iter_provider_stream(response, "openai")
            ]

    async def test_multiline_data_fields_form_one_json_event(self):
        response = _FakeLineResponse(
            [
                'data: {"choices": [',
                'data: {"delta": {"content": "hello"},',
                'data: "finish_reason": "stop"}]}',
                "",
                "data: [DONE]",
                "",
            ],
            auto_frame=False,
        )

        events = [event async for event in _iter_provider_stream(response, "openai")]

        self.assertEqual(1, len(events))
        self.assertEqual("hello", events[0]["content"])
        self.assertEqual("stop", events[0]["finish_reason"])

    async def test_two_complete_data_fields_without_blank_are_not_split(self):
        response = _FakeLineResponse(
            [
                'data: {"choices": []}',
                'data: {"choices": []}',
                "",
            ],
            auto_frame=False,
        )

        with self.assertRaises(httpx.RemoteProtocolError):
            _ = [
                event
                async for event in _iter_provider_stream(response, "openai")
            ]

    async def test_malformed_json_event_fails_closed(self):
        response = _FakeLineResponse(
            ["data: {not-json}", ""],
            auto_frame=False,
        )

        with self.assertRaises(httpx.RemoteProtocolError):
            _ = [
                event
                async for event in _iter_provider_stream(response, "openai")
            ]

    async def test_unterminated_data_event_fails_closed(self):
        response = _FakeLineResponse(
            ['data: {"choices": []}'],
            auto_frame=False,
        )

        with self.assertRaises(httpx.RemoteProtocolError):
            _ = [
                event
                async for event in _iter_provider_stream(response, "openai")
            ]

    async def test_choice_after_terminal_fails_closed(self):
        response = _FakeLineResponse([
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            }),
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "function": {
                                "name": "write_file",
                                "arguments": '{"filepath":"late.md"}',
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
        ])

        with self.assertRaises(httpx.RemoteProtocolError):
            _ = [
                event
                async for event in _iter_provider_stream(response, "openai")
            ]

    async def test_empty_choices_without_terminal_usage_fails_closed(self):
        for payload in (
            {"choices": []},
            {"choices": [], "usage": {"total_tokens": 1}},
        ):
            with self.subTest(payload=payload):
                response = _FakeLineResponse([
                    "data: " + json.dumps(payload),
                ])
                with self.assertRaises(httpx.RemoteProtocolError):
                    _ = [
                        event
                        async for event in _iter_provider_stream(
                            response,
                            "openai",
                        )
                    ]

    async def test_sse_event_bounds_fail_closed_before_dispatch(self):
        for constant, value, lines in (
            (
                "agent_loop._MAX_SSE_EVENT_DATA_LINES",
                1,
                ["data: {", "data: }", ""],
            ),
            (
                "agent_loop._MAX_SSE_EVENT_CHARS",
                4,
                ["data: 12345", ""],
            ),
        ):
            with self.subTest(constant=constant):
                response = _FakeLineResponse(lines, auto_frame=False)
                with (
                    patch(constant, value),
                    self.assertRaisesRegex(
                        httpx.RemoteProtocolError,
                        "limit",
                    ),
                ):
                    _ = [
                        event
                        async for event in _iter_provider_stream(
                            response,
                            "openai",
                        )
                    ]


class CorruptToolStreamRunTests(unittest.IsolatedAsyncioTestCase):
    _DELEGATE_RUN_ID = "run-verified-preload-fixture"
    _DELEGATE_USER_ID = "u-corrupt-stream-repair"
    _DELEGATE_SESSION_ID = "s-corrupt-stream-repair"
    _DELEGATE_WORKSPACE_SCOPE = "shared_session"

    @classmethod
    def _verified_preload_receipt(
        cls,
        *,
        source_count=1,
        kind_counts=None,
        aggregate_sha256=None,
        complete=True,
    ):
        return {
            "version": 1,
            "source_count": source_count,
            "kind_counts": kind_counts or {"skill_view": source_count},
            "aggregate_sha256": aggregate_sha256 or ("a" * 64),
            "complete": complete,
            "run_id": cls._DELEGATE_RUN_ID,
            "user_id": cls._DELEGATE_USER_ID,
            "session_id": cls._DELEGATE_SESSION_ID,
            "workspace_scope": cls._DELEGATE_WORKSPACE_SCOPE,
        }

    @staticmethod
    def _stream_lines(*, content="", finish_reason="tool_calls"):
        return [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": content,
                        "tool_calls": [{
                            "index": 0,
                            "id": "stream-secret-provider-id",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":"stream-secret',
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": finish_reason}],
            }),
            "data: [DONE]",
        ]

    async def _run_case(
        self,
        fallback_payload,
        *,
        content="",
        stream_finish_reason="tool_calls",
        turn_boundary_sink=None,
        debug_trace=True,
    ):
        requests = []
        lines = self._stream_lines(
            content=content,
            finish_reason=stream_finish_reason,
        )

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                requests.append({
                    "kind": "stream",
                    "method": method,
                    "url": url,
                    "body": kwargs.get("json"),
                })
                return _FakeLineResponse(lines)

            async def post(self, url, **kwargs):
                requests.append({
                    "kind": "fallback",
                    "url": url,
                    "body": kwargs.get("json"),
                })
                if isinstance(fallback_payload, BaseException):
                    raise fallback_payload
                if isinstance(fallback_payload, _FakeJSONResponse):
                    return fallback_payload
                return _FakeJSONResponse(fallback_payload)

        dispatch_mock = AsyncMock(return_value=json.dumps({"status": "ok"}))
        schemas = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "read",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        provider = {
            "id": "mock-tool-stream",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-tool-stream",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": False,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", dispatch_mock),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("skills.scanner.find_all_skills", return_value=[]),
                patch("agent_loop.settings.agent_debug_trace", debug_trace),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-tool-stream",
                        [{"role": "user", "content": "read a file"}],
                        ["read_file"],
                        provider_override=provider,
                        allow_session_mcp=False,
                        user_id="u-corrupt-stream",
                        session_id="s-corrupt-stream",
                        max_iterations=1,
                        turn_boundary_sink=turn_boundary_sink,
                    )
                ]
        return requests, dispatch_mock, events

    async def _run_stream_sequence(
        self,
        stream_batches,
        *,
        max_iterations=3,
        delegated=False,
        delegated_resource_boundary=False,
        allowed_read_paths=None,
        repair_fallback_payload=None,
        required_result_fields=None,
        required_result_schema=None,
        required_capability_tools=None,
        enabled_tool="read_file",
        task_text="read a file",
        verified_preloaded_input_receipt=None,
        persistent_tool_surface=False,
        required_tool_surface=False,
        requested_max_tokens=None,
        provider_context_length=64_000,
        fallback_overrides=None,
    ):
        """Run provider turns with an optional explicit-repair fallback."""
        requests = []
        remaining_batches = list(stream_batches)

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                requests.append({
                    "kind": "stream",
                    "method": method,
                    "url": url,
                    # The request body can be safely narrowed and retried when
                    # a compatible provider rejects an optional parameter.
                    # Capture the body at call time so the assertion observes
                    # the actual wire request, not the later mutated mapping.
                    "body": json.loads(json.dumps(kwargs.get("json"))),
                })
                if not remaining_batches:
                    raise AssertionError("unexpected extra streamed request")
                batch = remaining_batches.pop(0)
                if isinstance(batch, _FakeLineResponse):
                    return batch
                return _FakeLineResponse(batch)

            async def post(self, url, **kwargs):
                requests.append({
                    "kind": "fallback",
                    "url": url,
                    "body": json.loads(json.dumps(kwargs.get("json"))),
                })
                if repair_fallback_payload is None:
                    raise RuntimeError(
                        "mock explicit-repair transport fallback unavailable"
                    )
                if isinstance(repair_fallback_payload, BaseException):
                    raise repair_fallback_payload
                if isinstance(repair_fallback_payload, _FakeJSONResponse):
                    return repair_fallback_payload
                return _FakeJSONResponse(repair_fallback_payload)

        dispatch_mock = AsyncMock(return_value=json.dumps({"status": "ok"}))
        if not enabled_tool:
            schema_properties = {}
            schema_required = []
        elif enabled_tool == "web_search":
            schema_properties = {"query": {"type": "string"}}
            schema_required = ["query"]
        elif enabled_tool == "write_file":
            schema_properties = {
                "filepath": {"type": "string"},
                "content": {"type": "string"},
            }
            schema_required = ["filepath", "content"]
        else:
            schema_properties = {"filepath": {"type": "string"}}
            schema_required = ["filepath"]
        schemas = (
            [{
                "type": "function",
                "function": {
                    "name": enabled_tool,
                    "description": "bounded test capability",
                    "parameters": {
                        "type": "object",
                        "properties": schema_properties,
                        "required": schema_required,
                    },
                },
            }]
            if enabled_tool
            else []
        )
        provider = {
            "id": "mock-tool-stream-repair",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-tool-stream-repair",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": provider_context_length,
            "is_multimodal": False,
            "supports_thinking_toggle": True,
            "thinking_enabled_by_default": True,
        }

        direct_exposure_patch = (
            patch(
                "agent_loop._direct_chat_tool_exposure",
                return_value=DirectToolExposure(
                    tools=(enabled_tool,) if enabled_tool else (),
                    reasons=("test_persistent_tool_surface",),
                    required_groups=(
                        ((enabled_tool,),)
                        if required_tool_surface and enabled_tool
                        else ()
                    ),
                    missing_requirements=(),
                ),
            )
            if persistent_tool_surface or required_tool_surface
            else nullcontext()
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", dispatch_mock),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("skills.scanner.find_all_skills", return_value=[]),
                patch("agent_loop.settings.agent_debug_trace", True),
                direct_exposure_patch,
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-tool-stream-repair",
                        [{"role": "user", "content": task_text}],
                        [enabled_tool] if enabled_tool else [],
                        provider_override=provider,
                        allow_session_mcp=False,
                        user_id=self._DELEGATE_USER_ID,
                        session_id=self._DELEGATE_SESSION_ID,
                        run_id=self._DELEGATE_RUN_ID,
                        workspace_scope=self._DELEGATE_WORKSPACE_SCOPE,
                        max_iterations=max_iterations,
                        source="delegate" if delegated else "chat",
                        agent_kind="delegate" if delegated else "primary",
                        delegated_resource_boundary=(
                            delegated_resource_boundary
                        ),
                        allowed_read_paths=list(allowed_read_paths or []),
                        required_result_fields=list(
                            required_result_fields or []
                        ),
                        required_result_schema=dict(
                            required_result_schema or {}
                        ),
                        required_capability_tools=list(
                            required_capability_tools or []
                        ),
                        verified_preloaded_input_receipt=(
                            verified_preloaded_input_receipt
                        ),
                        max_tokens=requested_max_tokens,
                        fallback_overrides=fallback_overrides,
                    )
                ]
        return requests, dispatch_mock, events

    async def test_terminal_event_contains_reconciled_run_contract(self):
        _, dispatch_mock, events = await self._run_stream_sequence(
            [self._stop_lines("plain answer")],
            max_iterations=1,
            enabled_tool="",
            task_text="say hello",
        )

        dispatch_mock.assert_not_awaited()
        terminal = next(
            event
            for event in events
            if event.get("event_type") == "run.completed"
        )
        contract = terminal["payload"]["run_contract"]
        self.assertTrue(contract["terminal"])
        self.assertTrue(contract["reconciliation_valid"])
        self.assertRegex(
            contract["reconciliation_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual("verified", terminal["payload"]["contract_quality"])
        self.assertIn(
            "verifier.completed",
            [
                item["event"]
                for item in contract["lifecycle"]["recent_history"]
            ],
        )

    async def test_completion_receipt_failure_never_publishes_pass_or_success(self):
        with patch(
            "agent_loop.RunContractLedger.preview_terminal",
            return_value={
                "completion_allowed": False,
                "quality": "failed",
            },
        ):
            _, _, events = await self._run_stream_sequence(
                [self._stop_lines("plain answer")],
                max_iterations=1,
                enabled_tool="",
                task_text="say hello",
            )

        self.assertFalse(any(
            event.get("event_type") == "run.completed"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "verifier.completed"
            and (event.get("payload") or {}).get("verdict") == "pass"
            for event in events
        ))
        failed = next(
            event
            for event in events
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "completion_contract_failed",
            failed["payload"]["terminal_reason"],
        )

    async def test_invalid_lifecycle_integrity_cannot_publish_success(self):
        with patch(
            "agent_loop.RunLifecycleMachine.integrity_valid",
            new_callable=PropertyMock,
            return_value=False,
        ):
            _, _, events = await self._run_stream_sequence(
                [self._stop_lines("plain answer")],
                max_iterations=1,
                enabled_tool="",
                task_text="say hello",
            )

        self.assertFalse(any(
            event.get("event_type") == "run.completed"
            for event in events
        ))
        failed = next(
            event
            for event in events
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "lifecycle_integrity_failed",
            failed["payload"]["terminal_reason"],
        )

    async def test_live_runtime_capacity_clamps_the_first_wire_request(self):
        async def resolve_runtime(provider):
            resolved = dict(provider)
            resolved["context_length"] = 250_368
            return resolved, {
                "status": "runtime_catalog",
                "context_length": 250_368,
                "metadata_applied": True,
            }

        with (
            patch(
                "agent_loop.resolve_provider_runtime_metadata",
                side_effect=resolve_runtime,
            ),
            patch("agent_loop._estimate_payload_tokens", return_value=518),
        ):
            requests, dispatch_mock, events = await self._run_stream_sequence(
                [self._stop_lines("bounded answer")],
                max_iterations=1,
                enabled_tool="",
                task_text="answer directly",
                requested_max_tokens=262_144,
                provider_context_length=303_872,
            )

        dispatch_mock.assert_not_awaited()
        self.assertEqual(233_466, requests[0]["body"]["max_tokens"])
        metadata = [
            event["payload"]
            for event in events
            if event.get("event_type")
            == "debug.provider.metadata.resolved"
        ]
        self.assertEqual("initial_primary", metadata[0]["resolution_boundary"])
        self.assertEqual(250_368, metadata[0]["context_length"])

    async def test_overflow_feedback_updates_context_before_forced_compression(self):
        overflow_error = (
            "maximum context length is 10000 tokens; requested 256 output "
            "tokens; prompt contains at least 8800 input tokens"
        )
        with (
            patch(
                "agent_loop.ContextCompressor.set_context_length",
                autospec=True,
            ) as set_context_length,
            patch(
                "agent_loop.record_provider_context_limit",
            ) as record_context_limit,
        ):
            _requests, dispatch_mock, events = await self._run_stream_sequence(
                [_FakeHTTPErrorLineResponse(400, overflow_error)],
                max_iterations=1,
                enabled_tool="",
                task_text="answer directly",
                requested_max_tokens=256,
                provider_context_length=303_872,
            )

        dispatch_mock.assert_not_awaited()
        self.assertEqual(
            10_000,
            set_context_length.call_args.args[1],
        )
        self.assertEqual(
            10_000,
            record_context_limit.call_args.args[1],
        )
        corrected = [
            event["payload"]
            for event in events
            if event.get("event_type")
            == "debug.provider.metadata.corrected"
        ]
        self.assertEqual(1, len(corrected))
        self.assertEqual(10_000, corrected[0]["context_length"])
        self.assertIsNone(corrected[0]["effective_max_tokens"])
        self.assertEqual(
            "force_compression",
            corrected[0]["recovery_action"],
        )

    async def test_fallback_runtime_metadata_is_resolved_only_at_switch(self):
        resolved_ids = []

        async def resolve_runtime(provider):
            resolved_ids.append(str(provider.get("id") or ""))
            resolved = dict(provider)
            resolved["context_length"] = (
                120_000
                if resolved.get("id") == "fallback-runtime"
                else 250_368
            )
            return resolved, {
                "status": "runtime_catalog",
                "context_length": resolved["context_length"],
                "metadata_applied": True,
            }

        fallback = {
            "id": "fallback-runtime",
            "base_url": "http://fallback.invalid/v1",
            "api_model": "fallback-runtime",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 303_872,
            "is_multimodal": False,
        }
        with patch(
            "agent_loop.resolve_provider_runtime_metadata",
            side_effect=resolve_runtime,
        ):
            requests, dispatch_mock, events = await self._run_stream_sequence(
                [
                    _FakeHTTPErrorLineResponse(401, "unauthorized"),
                    self._stop_lines("fallback answer"),
                ],
                max_iterations=2,
                enabled_tool="",
                task_text="answer directly",
                fallback_overrides=[fallback],
            )

        dispatch_mock.assert_not_awaited()
        self.assertEqual(
            ["mock-tool-stream-repair", "fallback-runtime"],
            resolved_ids,
        )
        self.assertEqual(
            ["http://model.invalid/v1/chat/completions",
             "http://fallback.invalid/v1/chat/completions"],
            [request["url"] for request in requests],
        )
        metadata_boundaries = [
            event["payload"]["resolution_boundary"]
            for event in events
            if event.get("event_type")
            == "debug.provider.metadata.resolved"
        ]
        self.assertEqual(
            ["initial_primary", "provider_switch"],
            metadata_boundaries,
        )

    @staticmethod
    def _valid_tool_lines(
        value="clean.md",
        *,
        tool_name="read_file",
        argument_name=None,
        arguments=None,
    ):
        argument_name = argument_name or (
            "query" if tool_name == "web_search" else "filepath"
        )
        tool_arguments = (
            arguments
            if arguments is not None
            else {argument_name: value}
        )
        return [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "clean-call-id",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_arguments),
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

    @staticmethod
    def _structured_tool_lines(tool_name, arguments):
        return [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "structured-call-id",
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

    @staticmethod
    def _valid_parallel_tool_lines():
        return [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "clean-call-id-0",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({
                                        "filepath": "first.md",
                                    }),
                                },
                            },
                            {
                                "index": 1,
                                "id": "clean-call-id-1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({
                                        "filepath": "second.md",
                                    }),
                                },
                            },
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

    @staticmethod
    def _stop_lines(content="done"):
        return [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": content},
                    "finish_reason": "stop",
                }],
            }),
            "data: [DONE]",
        ]

    @staticmethod
    def _length_lines(content, *, reasoning=None):
        delta = {"content": content}
        if reasoning is not None:
            delta["reasoning_content"] = reasoning
        return [
            "data: " + json.dumps({
                "choices": [{
                    "delta": delta,
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]

    @staticmethod
    def _partial_corrupt_lines(
        *,
        content="safe-visible-anchor",
        reasoning="hidden-reasoning-secret",
        argument_secret="raw-corrupt-argument-secret",
        tool_name="read_file",
        argument_name=None,
    ):
        argument_name = argument_name or (
            "query" if tool_name == "web_search" else "filepath"
        )
        return [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": content,
                        "reasoning_content": reasoning,
                        "tool_calls": [{
                            "index": 0,
                            "id": "corrupt-provider-id",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": (
                                    '{"' + argument_name + '":"'
                                    + argument_secret
                                ),
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

    async def test_partial_corrupt_continuation_then_valid_batch_dispatches_once(self):
        requests, dispatch_mock, events = await self._run_stream_sequence([
            self._partial_corrupt_lines(),
            self._valid_tool_lines(),
            self._stop_lines(),
        ])

        self.assertEqual(["stream", "stream", "stream"], [
            request["kind"] for request in requests
        ])
        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            {"filepath": "clean.md"},
            dispatch_mock.await_args.args[1],
        )
        self.assertEqual(
            requests[0]["body"]["tools"],
            requests[1]["body"]["tools"],
        )
        self.assertNotIn("parallel_tool_calls", requests[0]["body"])
        self.assertEqual("required", requests[1]["body"].get("tool_choice"))
        self.assertIs(False, requests[1]["body"].get("parallel_tool_calls"))
        # read_file can carry a large path/result contract and is not in the
        # bounded-argument capability set. Structural repair remains atomic
        # and thinking-off without shrinking its ordinary output budget.

        self.assertEqual(8192, requests[1]["body"].get("max_tokens"))
        self.assertEqual(
            {"enable_thinking": False},
            requests[1]["body"].get("chat_template_kwargs"),
        )
        self.assertNotIn("parallel_tool_calls", requests[2]["body"])
        self.assertEqual("done", events[-1].get("type"))
        self.assertEqual("stop", events[-1].get("finish_reason"))

        repair_gate = next(
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "tool_stream_continuation_repair"
        )
        self.assertEqual(1, repair_gate["payload"]["repair_count"])
        self.assertEqual(1, repair_gate["payload"]["max_repairs"])
        self.assertEqual(
            1,
            repair_gate["payload"]["required_logical_call_count"],
        )
        self.assertTrue(
            repair_gate["payload"]["bounded_atomic_tool_call_turn"]
        )
        self.assertTrue(
            repair_gate["payload"][
                "structural_atomic_tool_stream_repair"
            ]
        )
        self.assertEqual(8192, repair_gate["payload"]["max_tokens"])
        self.assertFalse(
            repair_gate["payload"]["bounded_argument_tool_stream_repair"]
        )
        self.assertTrue(
            repair_gate["payload"]["thinking_disabled_when_supported"]
        )
        repair_request = next(
            event for event in events
            if event.get("event_type") == "debug.llm.request"
            and event.get("payload", {}).get("tool_stream_continuation_repair")
        )
        self.assertTrue(
            repair_request["payload"]["bounded_atomic_tool_call_turn"]
        )
        self.assertTrue(
            repair_request["payload"][
                "structural_atomic_tool_stream_repair"
            ]
        )
        self.assertFalse(
            repair_request["payload"]["bounded_tool_stream_repair"]
        )
        self.assertFalse(
            repair_request["payload"][
                "bounded_argument_tool_stream_repair"
            ]
        )
        self.assertTrue(
            repair_request["payload"]["repair_tool_choice_required"]
        )
        self.assertTrue(
            repair_request["payload"]["repair_parallel_calls_forbidden"]
        )
        self.assertTrue(repair_request["payload"]["thinking_disabled"])
        self.assertEqual(8192, repair_request["payload"]["requested_max_tokens"])
        self.assertEqual(8192, repair_request["payload"]["effective_max_tokens"])

    async def test_done_without_authoritative_finish_never_dispatches(self):
        incomplete = _FakeLineResponse(
            [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": "must-not-dispatch",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({
                                        "filepath": "changed-by-truncation.md",
                                    }),
                                },
                            }],
                        },
                        "finish_reason": None,
                    }],
                }),
                "",
                "data: [DONE]",
                "",
            ],
            auto_frame=False,
        )

        with patch("agent_loop.asyncio.sleep", AsyncMock(return_value=None)):
            requests, dispatch_mock, events = await self._run_stream_sequence(
                [incomplete, incomplete, incomplete],
                max_iterations=1,
            )

        # A structured tool fragment is material provider output even when no
        # visible text was emitted. Replaying this attempt could later
        # dispatch a duplicated call, so fail closed after the first broken
        # stream instead of spending the transport retry budget.
        self.assertEqual(1, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertEqual("error", events[-1]["type"])
        self.assertIn(
            "did not transparently replay",
            events[-1]["msg"],
        )

    async def test_bounded_http_repair_caps_arguments_at_2048(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(
                    tool_name="web_search",
                    argument_name="query",
                ),
                self._valid_tool_lines(
                    "bounded repair query",
                    tool_name="web_search",
                ),
                self._stop_lines("bounded search complete"),
            ],
            enabled_tool="web_search",
            delegated=True,
        )

        self.assertEqual(["stream", "stream", "stream"], [
            request["kind"] for request in requests
        ])
        repair_body = requests[1]["body"]
        self.assertEqual(requests[0]["body"]["tools"], repair_body["tools"])
        self.assertEqual(2048, repair_body.get("max_tokens"))
        self.assertEqual("required", repair_body.get("tool_choice"))
        self.assertIs(False, repair_body.get("parallel_tool_calls"))
        self.assertEqual(
            {"enable_thinking": False},
            repair_body.get("chat_template_kwargs"),
        )
        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            {"query": "bounded repair query"},
            dispatch_mock.await_args.args[1],
        )
        repair_request = next(
            event for event in events
            if event.get("event_type") == "debug.llm.request"
            and event.get("payload", {}).get("tool_stream_continuation_repair")
        )
        self.assertTrue(
            repair_request["payload"]["bounded_atomic_tool_call_turn"]
        )
        self.assertTrue(
            repair_request["payload"][
                "bounded_argument_tool_stream_repair"
            ]
        )
        self.assertEqual(2048, repair_request["payload"]["effective_max_tokens"])

    async def test_large_argument_write_repair_keeps_normal_budget_thinking_off(self):
        # write_file is not a read-only tool, so no batch here is ever
        # salvageable; every corrupt sample burns one no-progress recovery
        # until the bounded ladder (3 consecutive no-progress recoveries) is
        # exhausted with no trusted tool result available for synthesis.
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(
                    tool_name="write_file",
                    argument_name="content",
                ),
                self._partial_corrupt_lines(
                    content="second-corrupt-write-anchor",
                    tool_name="write_file",
                    argument_name="content",
                ),
                self._partial_corrupt_lines(
                    content="third-corrupt-write-anchor",
                    tool_name="write_file",
                    argument_name="content",
                ),
                self._partial_corrupt_lines(
                    content="fourth-corrupt-write-anchor",
                    tool_name="write_file",
                    argument_name="content",
                ),
                self._partial_corrupt_lines(
                    content="fifth-corrupt-write-anchor",
                    tool_name="write_file",
                    argument_name="content",
                ),
            ],
            enabled_tool="write_file",
            delegated=True,
            max_iterations=15,
        )

        self.assertEqual(
            [
                "stream", "stream", "fallback", "stream", "fallback",
                "stream", "fallback",
            ],
            [request["kind"] for request in requests],
        )
        repair_body = requests[1]["body"]
        self.assertEqual(8192, repair_body.get("max_tokens"))
        self.assertEqual("required", repair_body.get("tool_choice"))
        self.assertIs(False, repair_body.get("parallel_tool_calls"))
        self.assertEqual(
            {"enable_thinking": False},
            repair_body.get("chat_template_kwargs"),
        )
        dispatch_mock.assert_not_awaited()
        repair_request = next(
            event for event in events
            if event.get("event_type") == "debug.llm.request"
            and event.get("payload", {}).get("tool_stream_continuation_repair")
        )
        self.assertTrue(
            repair_request["payload"]["bounded_atomic_tool_call_turn"]
        )
        self.assertFalse(
            repair_request["payload"][
                "bounded_argument_tool_stream_repair"
            ]
        )
        terminal = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_recovery_exhausted",
            terminal["payload"]["finish_reason"],
        )
        self.assertEqual(
            "consecutive_no_progress_limit_exhausted",
            terminal["payload"]["replan_unavailable_reason"],
        )

    async def test_visible_length_after_tool_stream_repair_closes_once(self):
        visible_length = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": (
                            "status: WARN\nEvidence: repaired call result retained"
                        ),
                    },
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(),
                self._valid_tool_lines(),
                visible_length,
                self._stop_lines(
                    "Conclusion: retain WARN with the repaired-call provenance."
                ),
            ],
            max_iterations=6,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ))
        self.assertNotIn("tools", requests[-1]["body"])
        self.assertEqual(events[-1]["type"], "done")

    async def test_repair_multi_call_batch_salvages_and_dispatches_both_calls(self):
        # A two-call repair batch is no longer discarded wholesale: both calls
        # are read-only, so they are salvaged from the "wrong call count"
        # batch. The first dispatches immediately; the second is unresolved
        # (the repair gate expects exactly one call) and is replanned once
        # under the same closed schema, then dispatched too.
        requests, dispatch_mock, events = await self._run_stream_sequence([
            self._partial_corrupt_lines(),
            self._valid_parallel_tool_lines(),
            self._valid_tool_lines("second.md"),
            self._stop_lines("done"),
        ], max_iterations=10)

        self.assertEqual(["stream", "stream", "stream", "stream"], [
            request["kind"] for request in requests
        ])
        self.assertIs(False, requests[1]["body"].get("parallel_tool_calls"))
        self.assertEqual(2, dispatch_mock.await_count)
        self.assertEqual(
            [{"filepath": "first.md"}, {"filepath": "second.md"}],
            [call.args[1] for call in dispatch_mock.await_args_list],
        )
        salvage_accepted = [
            event for event in events
            if event.get("event_type") == "debug.tool.stream.salvage.accepted"
        ]
        self.assertEqual(1, len(salvage_accepted))
        self.assertEqual(
            "repair_call_count_mismatch",
            salvage_accepted[0]["payload"]["source"],
        )
        self.assertTrue(any(
            event.get("event_type") == "run.completed" for event in events
        ))

    async def test_rejected_parallel_hint_keeps_local_exact_one_gate(self):
        requests, dispatch_mock, events = await self._run_stream_sequence([
            self._partial_corrupt_lines(),
            _FakeHTTPErrorLineResponse(
                400,
                '{"error":"parallel_tool_calls is not supported"}',
            ),
            self._valid_tool_lines("after-compatible-retry.md"),
            self._stop_lines(),
        ])

        self.assertEqual(["stream", "stream", "stream", "stream"], [
            request["kind"] for request in requests
        ])
        self.assertIs(False, requests[1]["body"].get("parallel_tool_calls"))
        self.assertNotIn("parallel_tool_calls", requests[2]["body"])
        self.assertEqual("required", requests[2]["body"].get("tool_choice"))
        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            {"filepath": "after-compatible-retry.md"},
            dispatch_mock.await_args.args[1],
        )
        fallback_debug = next(
            event for event in events
            if event.get("event_type")
            == (
                "debug.tool.stream.continuation_repair."
                "parallel_parameter_rejected"
            )
        )
        self.assertEqual(
            "omit_parameter_keep_exact_one_gate",
            fallback_debug["payload"]["fallback"],
        )

    async def test_rejected_parallel_hint_salvages_and_dispatches_both_calls(self):
        # Even after the parallel_tool_calls compat fallback, a two-call batch
        # is salvaged (both calls are read-only) rather than discarded: the
        # first call dispatches immediately and the second is replanned once
        # under the same closed schema before it also dispatches.
        requests, dispatch_mock, events = await self._run_stream_sequence([
            self._partial_corrupt_lines(),
            _FakeHTTPErrorLineResponse(
                400,
                '{"error":"unsupported parameter parallel_tool_calls"}',
            ),
            self._valid_parallel_tool_lines(),
            self._valid_tool_lines("second.md"),
            self._stop_lines("done"),
        ], max_iterations=10)

        self.assertEqual(5, len(requests))
        self.assertIs(False, requests[1]["body"].get("parallel_tool_calls"))
        self.assertNotIn("parallel_tool_calls", requests[2]["body"])
        self.assertEqual(2, dispatch_mock.await_count)
        self.assertEqual(
            [{"filepath": "first.md"}, {"filepath": "second.md"}],
            [call.args[1] for call in dispatch_mock.await_args_list],
        )
        self.assertTrue(any(
            event.get("event_type") == "run.completed" for event in events
        ))

    async def test_second_corrupt_batch_after_continuation_is_terminal(self):
        # A second corrupt batch no longer fails closed immediately: each
        # further corrupt repair-turn sample first gets one bounded exact-one
        # replan attempt (progress_made=False each time, since every sample is
        # corrupt), consuming the no-progress recovery budget. Only once that
        # budget (_MAX_TOOL_STREAM_CONSECUTIVE_NO_PROGRESS_RECOVERIES == 3) is
        # exhausted, with no trusted tool result to synthesize from, does the
        # run terminate.
        requests, dispatch_mock, events = await self._run_stream_sequence([
            self._partial_corrupt_lines(),
            self._partial_corrupt_lines(
                content="second-safe-anchor",
                reasoning="second-hidden-reasoning",
                argument_secret="second-raw-argument",
            ),
            self._partial_corrupt_lines(
                content="third-safe-anchor",
                reasoning="third-hidden-reasoning",
                argument_secret="third-raw-argument",
            ),
            self._partial_corrupt_lines(
                content="fourth-safe-anchor",
                reasoning="fourth-hidden-reasoning",
                argument_secret="fourth-raw-argument",
            ),
            self._partial_corrupt_lines(
                content="fifth-safe-anchor",
                reasoning="fifth-hidden-reasoning",
                argument_secret="fifth-raw-argument",
            ),
        ], max_iterations=10)

        self.assertEqual(
            [
                "stream", "stream", "fallback", "stream", "fallback",
                "stream", "fallback",
            ],
            [request["kind"] for request in requests],
        )
        self.assertEqual("required", requests[1]["body"].get("tool_choice"))
        self.assertIs(False, requests[1]["body"].get("parallel_tool_calls"))
        self.assertEqual(8192, requests[1]["body"].get("max_tokens"))
        self.assertEqual(
            {"enable_thinking": False},
            requests[1]["body"].get("chat_template_kwargs"),
        )
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "tool.dispatch_started"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "run.completed"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_recovery_exhausted",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(3, failed["payload"]["run_recovery_count"])
        self.assertEqual(
            3, failed["payload"]["consecutive_no_progress_recoveries"]
        )
        self.assertEqual(
            "consecutive_no_progress_limit_exhausted",
            failed["payload"]["replan_unavailable_reason"],
        )
        self.assertEqual(
            "no_trusted_successful_tool_result",
            failed["payload"]["synthesis_unavailable_reason"],
        )
        terminal_usage = failed["payload"]["usage"]
        self.assertGreater(terminal_usage["input_tokens"], 0)
        self.assertGreater(terminal_usage["output_tokens"], 0)
        self.assertGreaterEqual(
            terminal_usage["total_tokens"],
            terminal_usage["input_tokens"]
            + terminal_usage["output_tokens"],
        )
        repair_failures = [
            event for event in events
            if event.get("event_type")
            == "debug.tool.stream.continuation_repair.failed"
        ]
        self.assertEqual(3, len(repair_failures))
        for repair_failure in repair_failures:
            self.assertEqual(
                "corrupt_replan_sample",
                repair_failure["payload"]["reason"],
            )
            self.assertTrue(
                repair_failure["payload"]["bounded_atomic_tool_call_turn"]
            )

    async def test_new_corruption_after_repaired_dispatch_gets_new_episode(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(),
                self._valid_tool_lines("first.md"),
                self._partial_corrupt_lines(
                    content="later-safe-anchor",
                    reasoning="later-hidden-reasoning",
                    argument_secret="later-truncated-argument",
                ),
                self._valid_tool_lines("second.md"),
                self._stop_lines("done after two independent repairs"),
            ],
            max_iterations=5,
            persistent_tool_surface=True,
        )

        self.assertEqual(["stream"] * 5, [item["kind"] for item in requests])
        self.assertEqual(2, dispatch_mock.await_count)
        self.assertEqual(
            [{"filepath": "first.md"}, {"filepath": "second.md"}],
            [call.args[1] for call in dispatch_mock.await_args_list],
        )
        requested = [
            event for event in events
            if event.get("event_type")
            == "debug.tool.stream.continuation_repair.requested"
        ]
        resolved = [
            event for event in events
            if event.get("event_type")
            == "debug.tool.stream.continuation_repair.resolved"
        ]
        self.assertEqual([1, 2], [
            event["payload"]["repair_episode"] for event in requested
        ])
        self.assertEqual([1, 2], [
            event["payload"]["repair_episode"] for event in resolved
        ])
        self.assertEqual("done", events[-1].get("type"))

    async def test_preflight_rejected_repair_still_closes_corruption_episode(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(),
                self._valid_tool_lines({"invalid": "schema-shape"}),
                self._partial_corrupt_lines(
                    content="independent-later-anchor",
                    argument_secret="independent-later-truncation",
                ),
                self._valid_tool_lines("eventually-valid.md"),
                self._stop_lines("done after isolated preflight failure"),
            ],
            max_iterations=5,
            persistent_tool_surface=True,
        )

        self.assertEqual(["stream"] * 5, [item["kind"] for item in requests])
        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            {"filepath": "eventually-valid.md"},
            dispatch_mock.await_args.args[1],
        )
        requested = [
            event
            for event in events
            if event.get("event_type")
            == "debug.tool.stream.continuation_repair.requested"
        ]
        resolved = [
            event
            for event in events
            if event.get("event_type")
            == "debug.tool.stream.continuation_repair.resolved"
        ]
        self.assertEqual([1, 2], [
            event["payload"]["repair_episode"] for event in requested
        ])
        self.assertEqual([1, 2], [
            event["payload"]["repair_episode"] for event in resolved
        ])
        self.assertTrue(all(
            event["payload"]["structural_assembly_valid"]
            for event in resolved
        ))
        self.assertEqual("done", events[-1].get("type"))

    async def test_repair_reasoning_cycle_is_bounded_and_fails_closed(self):
        cycle = (
            "Plan the same call, reconsider its arguments, and continue "
            "reasoning without emitting the required tool envelope. "
        ) * 12
        reasoning_cycle = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": cycle * 12},
                    "finish_reason": None,
                }],
            }),
            "data: [DONE]",
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(
                    tool_name="web_search",
                    argument_name="query",
                ),
                reasoning_cycle,
            ],
            max_iterations=4,
            delegated=True,
            enabled_tool="web_search",
        )

        self.assertEqual(["stream", "stream"], [
            request["kind"] for request in requests
        ])
        repair_body = requests[1]["body"]
        self.assertEqual(2048, repair_body.get("max_tokens"))
        self.assertEqual("required", repair_body.get("tool_choice"))
        self.assertIs(False, repair_body.get("parallel_tool_calls"))
        self.assertEqual(
            {"enable_thinking": False},
            repair_body.get("chat_template_kwargs"),
        )
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") in {"tool.dispatch_started", "run.completed"}
            for event in events
        ))
        abort = next(
            event for event in events
            if event.get("event_type") == "debug.llm.stream_convergence_aborted"
        )
        self.assertEqual("reasoning_cycle_detected", abort["payload"]["reason"])
        self.assertTrue(
            abort["payload"]["bounded_atomic_tool_call_turn"]
        )
        repair_failure = next(
            event for event in events
            if event.get("event_type")
            == "debug.tool.stream.continuation_repair.failed"
        )
        self.assertEqual(
            "stream_convergence_aborted",
            repair_failure["payload"]["reason"],
        )
        self.assertEqual(
            "reasoning_cycle_detected",
            repair_failure["payload"]["stream_convergence_reason"],
        )
        terminal = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_stream_convergence_failed",
            terminal["payload"]["finish_reason"],
        )
        self.assertEqual("error", events[-1]["type"])

    async def test_repair_does_not_relax_rejected_required_tool_choice(self):
        requests, dispatch_mock, events = await self._run_stream_sequence([
            self._partial_corrupt_lines(
                tool_name="web_search",
                argument_name="query",
            ),
            _FakeHTTPErrorLineResponse(
                400,
                '{"error":"tool_choice is not supported"}',
            ),
        ], enabled_tool="web_search", delegated=True)

        self.assertEqual(["stream", "stream"], [
            request["kind"] for request in requests
        ])
        self.assertEqual("required", requests[1]["body"].get("tool_choice"))
        self.assertEqual(2048, requests[1]["body"].get("max_tokens"))
        dispatch_mock.assert_not_awaited()
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_repair_not_emitted",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(
            "required_tool_choice_rejected",
            failed["payload"]["reason"],
        )
        self.assertIs(False, failed["payload"]["actual_dispatch_attempted"])
        self.assertFalse(any(
            event.get("event_type") == "run.completed" for event in events
        ))

    async def test_corrupt_explicit_repair_uses_one_nonstream_replacement(self):
        fallback_payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": "fallback-provider-content-secret",
                    "reasoning_content": "fallback-provider-reasoning-secret",
                    "tool_calls": [{
                        "id": "replacement-call-id",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({
                                "filepath": "replacement-clean.md",
                            }),
                        },
                    }],
                },
            }],
            "usage": {
                "prompt_tokens": 17,
                "completion_tokens": 321,
                "total_tokens": 338,
            },
        }
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(
                    content="repair-stream-content-secret",
                    reasoning="repair-stream-reasoning-secret",
                    argument_secret="repair-stream-argument-secret",
                ),
                self._stop_lines("done after replacement"),
            ],
            max_iterations=3,
            repair_fallback_payload=fallback_payload,
        )

        self.assertEqual(
            ["stream", "stream", "fallback", "stream"],
            [request["kind"] for request in requests],
        )
        repair_body = dict(requests[1]["body"])
        expected_fallback_body = dict(repair_body)
        expected_fallback_body["stream"] = False
        expected_fallback_body.pop("stream_options", None)
        self.assertEqual(expected_fallback_body, requests[2]["body"])
        self.assertEqual("required", requests[2]["body"]["tool_choice"])
        self.assertIs(False, requests[2]["body"]["parallel_tool_calls"])

        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            {"filepath": "replacement-clean.md"},
            dispatch_mock.await_args.args[1],
        )
        iteration_events = [
            event for event in events
            if event.get("event_type") == "debug.iteration.started"
        ]
        self.assertEqual(3, len(iteration_events))
        self.assertEqual(
            [1, 2, 3],
            [event["payload"]["iteration"] for event in iteration_events],
        )
        usage_event = next(
            event for event in reversed(events)
            if event.get("type") == "usage"
        )
        self.assertGreaterEqual(usage_event["output_tokens"], 321)

        fallback_events = [
            event for event in events
            if str(event.get("event_type") or "").startswith(
                "debug.tool.stream.continuation_repair.transport_fallback."
            )
        ]
        self.assertEqual(
            [
                "debug.tool.stream.continuation_repair."
                "transport_fallback.requested",
                "debug.tool.stream.continuation_repair."
                "transport_fallback.completed",
            ],
            [event["event_type"] for event in fallback_events],
        )
        self.assertEqual(
            1,
            fallback_events[0]["payload"]["transport_replacement_count"],
        )
        self.assertEqual(
            1,
            fallback_events[0]["payload"]["max_transport_replacements"],
        )
        self.assertIs(
            False,
            fallback_events[0]["payload"]["iteration_budget_consumed"],
        )
        event_dump = json.dumps(events, ensure_ascii=False)
        next_request_history = json.dumps(
            requests[3]["body"]["messages"], ensure_ascii=False
        )
        for secret in (
            "repair-stream-content-secret",
            "repair-stream-reasoning-secret",
            "repair-stream-argument-secret",
            "fallback-provider-content-secret",
            "fallback-provider-reasoning-secret",
        ):
            self.assertNotIn(secret, event_dump)
            self.assertNotIn(secret, next_request_history)

    async def test_nonstream_repair_replacement_keeps_exact_one_gate(self):
        fallback_payload = {
            "choices": [{
                "message": {
                    "tool_calls": [
                        {
                            "id": "replacement-call-0",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({
                                    "filepath": "first.md",
                                }),
                            },
                        },
                        {
                            "id": "replacement-call-1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({
                                    "filepath": "second.md",
                                }),
                            },
                        },
                    ],
                },
            }],
        }
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="discarded repair text"),
                self._valid_tool_lines("second.md"),
                self._stop_lines("done"),
            ],
            repair_fallback_payload=fallback_payload,
            max_iterations=5,
        )

        self.assertEqual(
            ["stream", "stream", "fallback", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        self.assertEqual(2, dispatch_mock.await_count)
        self.assertEqual(
            [{"filepath": "first.md"}, {"filepath": "second.md"}],
            [call.args[1] for call in dispatch_mock.await_args_list],
        )
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.tool.stream.salvage.accepted"
            for event in events
        ))
        self.assertTrue(any(
            event.get("event_type") == "run.completed"
            for event in events
        ))

    async def test_nonstream_repair_replacement_keeps_pure_preflight(self):
        fallback_payload = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "replacement-boundary-call",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({
                                "filepath": "outside-compiled-closure.md",
                            }),
                        },
                    }],
                },
            }],
        }
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="discarded repair text"),
            ],
            max_iterations=2,
            delegated=True,
            delegated_resource_boundary=True,
            allowed_read_paths=[],
            repair_fallback_payload=fallback_payload,
        )

        self.assertEqual(
            ["stream", "stream", "fallback"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_not_awaited()
        rejected_result = next(
            event for event in events
            if event.get("event_type") == "tool.failed"
        )
        self.assertIs(
            False,
            rejected_result["payload"]["actual_dispatch_attempted"],
        )
        self.assertFalse(any(
            event.get("event_type") == "tool.dispatch_started"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
            for event in events
        ))

    async def test_nonstream_repair_transport_failure_is_bounded(self):
        # Every non-stream repair attempt hits the same transport failure, so
        # no batch is ever salvageable via the fallback path either; each
        # corrupt sample burns one no-progress recovery until the bounded
        # ladder (3 consecutive no-progress recoveries) is exhausted with no
        # trusted tool result available for synthesis.
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="second-corrupt-anchor"),
                self._partial_corrupt_lines(content="third-corrupt-anchor"),
                self._partial_corrupt_lines(content="fourth-corrupt-anchor"),
                self._partial_corrupt_lines(content="fifth-corrupt-anchor"),
            ],
            repair_fallback_payload=_FakeJSONResponse({}, status_code=503),
            max_iterations=15,
        )

        self.assertEqual(
            [
                "stream", "stream", "fallback", "stream", "fallback",
                "stream", "fallback",
            ],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_not_awaited()
        failed_transport = [
            event for event in events
            if event.get("event_type")
            == (
                "debug.tool.stream.continuation_repair."
                "transport_fallback.failed"
            )
        ]
        self.assertEqual(3, len(failed_transport))
        self.assertEqual(
            "http_error", failed_transport[0]["payload"]["failure_kind"]
        )
        self.assertEqual(503, failed_transport[0]["payload"]["http_status"])
        iteration_events = [
            event for event in events
            if event.get("event_type") == "debug.iteration.started"
        ]
        self.assertEqual(4, len(iteration_events))
        terminal = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_recovery_exhausted",
            terminal["payload"]["finish_reason"],
        )
        self.assertEqual(
            "consecutive_no_progress_limit_exhausted",
            terminal["payload"]["replan_unavailable_reason"],
        )

    async def test_delegated_post_dispatch_repair_failure_synthesizes_once(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(
                    content="first-safe-anchor",
                    reasoning="first-hidden-reasoning",
                    argument_secret="first-corrupt-secret",
                ),
                self._partial_corrupt_lines(
                    content="second-discarded-anchor",
                    reasoning="second-hidden-reasoning",
                    argument_secret="second-corrupt-secret",
                ),
                self._stop_lines(
                    "status: WARN/degraded\nEvidence: evidence.md\n"
                    "Gap: later provider tool batch was unavailable\n"
                    "RESULT_FIELDS_JSON: {}"
                ),
            ],
            max_iterations=5,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertIn("tools", requests[0]["body"])
        self.assertIn("tools", requests[1]["body"])
        self.assertEqual("required", requests[2]["body"].get("tool_choice"))
        self.assertFalse(requests[3]["body"]["stream"])
        self.assertNotIn("tools", requests[4]["body"])
        recovery_history = json.dumps(
            requests[4]["body"]["messages"], ensure_ascii=False
        )
        self.assertIn("Earlier tool results", recovery_history)
        self.assertIn("evidence.md", recovery_history)
        self.assertNotIn("first-corrupt-secret", recovery_history)
        self.assertNotIn("first-hidden-reasoning", recovery_history)
        self.assertNotIn("second-corrupt-secret", recovery_history)
        self.assertNotIn("second-hidden-reasoning", recovery_history)
        recovery_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
        ]
        self.assertEqual(1, len(recovery_gates))
        self.assertEqual(
            1,
            recovery_gates[0]["payload"]["dispatched_tool_result_count"],
        )
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        emitted = next(
            event for event in events
            if event.get("event_type")
            == "debug.tool.stream.post_dispatch_synthesis.emitted"
        )
        self.assertGreater(emitted["payload"]["content_chars"], 0)
        self.assertEqual(
            {"type": "done", "finish_reason": "stop"},
            events[-1],
        )

    async def test_post_dispatch_invalid_result_gets_one_closed_contract_repair(self):
        invalid_result = (
            "pending synthesis\n"
            "<tool_call><arguments>{\"query\":\"retry\"}</arguments></tool_call>"
        )
        clean_fields = {
            "NCT_ID": {
                "status": "degraded",
                "reason": "not present in fixture",
                "provenance": "attempted source/fallback",
            },
        }
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                self._stop_lines(invalid_result),
                self._structured_tool_lines(
                    "submit_result_fields",
                    clean_fields,
                ),
            ],
            max_iterations=6,
            delegated=True,
            required_result_fields=["NCT_ID"],
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            "submit_result_fields",
            requests[-1]["body"]["tools"][0]["function"]["name"],
        )
        repair_history = json.dumps(
            requests[-1]["body"]["messages"],
            ensure_ascii=False,
        )
        self.assertIn("bounded typed-result projector", repair_history)
        self.assertIn("evidence.md", repair_history)
        self.assertNotIn("pending synthesis", repair_history)
        self.assertNotIn("<arguments>", repair_history)
        repair_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_result_footer_repair"
        ]
        self.assertEqual(1, len(repair_gates))
        completed = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.completed"
        )
        self.assertTrue(completed["payload"]["footer_valid"])
        terminal = [
            event for event in events
            if event.get("event_type") == "run.completed"
        ][-1]
        self.assertEqual(
            "delegated_result_footer_structured_repair",
            terminal["payload"]["terminal_reason"],
        )
        self.assertEqual(
            {"type": "done", "finish_reason": "stop"},
            events[-1],
        )

    async def test_raw_protocol_without_dispatch_gets_one_closed_contract_repair(self):
        invalid_result = (
            "report draft\n"
            "<tool_call><name>execute_code</name><arguments>"
            "{\"code\":\"print(1)\"}</arguments></tool_call>"
        )
        clean_result = (
            "# Evidence report\n"
            "No executable capability was used. The unavailable fact is an "
            "explicit WARN/degraded gap with task-context provenance."
        )
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._stop_lines(invalid_result),
                self._stop_lines(clean_result),
            ],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(["stream", "stream"], [
            request["kind"] for request in requests
        ])
        dispatch_mock.assert_not_awaited()
        self.assertIn("tools", requests[0]["body"])
        self.assertNotIn("tools", requests[1]["body"])
        repair_history = json.dumps(
            requests[1]["body"]["messages"],
            ensure_ascii=False,
        )
        self.assertIn("output-contract", repair_history)
        self.assertNotIn("execute_code", repair_history)
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_output_contract_repair"
            for event in events
        ))
        completed = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.output_contract_repair.completed"
        )
        self.assertEqual(0, completed["payload"]["raw_protocol_count"])

    async def test_output_contract_repair_uses_isolated_structured_submitter(self):
        invalid_result = (
            "rejected draft\n"
            "<tool_call><name>execute_code</name><arguments>"
            '{"code":"print(1)"}</arguments></tool_call>'
        )
        fields = {
            "study_title": {
                "status": "degraded",
                "reason": "not present in retained evidence",
                "provenance": "attempted source/fallback",
            },
        }

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._stop_lines(invalid_result),
                self._structured_tool_lines(
                    "submit_result_fields",
                    fields,
                ),
            ],
            max_iterations=4,
            delegated=True,
            required_result_fields=["study_title"],
        )

        self.assertEqual(2, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertEqual(
            "submit_result_fields",
            requests[1]["body"]["tools"][0]["function"]["name"],
        )
        self.assertEqual("required", requests[1]["body"]["tool_choice"])
        self.assertEqual(2, len(requests[1]["body"]["messages"]))
        repair_history = json.dumps(
            requests[1]["body"]["messages"], ensure_ascii=False
        )
        self.assertNotIn(invalid_result, repair_history)
        self.assertIn("REJECTED_DRAFT_OMITTED", repair_history)
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_result_footer_repair"
            for event in events
        ))
        completed = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.completed"
        )
        self.assertTrue(completed["payload"]["footer_valid"])
        self.assertFalse(completed["payload"]["registry_dispatch_attempted"])
        terminal = [
            event for event in events
            if event.get("event_type") == "run.completed"
        ][-1]
        self.assertEqual(
            "delegated_result_footer_structured_repair",
            terminal["payload"]["terminal_reason"],
        )
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))

    async def test_output_contract_repair_second_length_discards_both_samples(self):
        invalid_result = (
            "rejected draft\n"
            "<tool_call><arguments>{}</arguments></tool_call>"
        )
        repair_prefix = "# Buffered repair\nFirst clean bounded evidence block."
        second_partial = "Still incomplete bounded conclusion."

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._stop_lines(invalid_result),
                self._length_lines(repair_prefix),
                self._length_lines(second_partial),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=5,
            delegated=True,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_not_awaited()
        emitted = [
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        ]
        self.assertNotIn(repair_prefix, emitted)
        self.assertNotIn(second_partial, emitted)
        failed = next(
            event for event in events
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "delegated_output_contract_repair_continuation_failed",
            failed["payload"]["finish_reason"],
        )
        self.assertTrue(failed["payload"]["transactional_buffering"])
        self.assertEqual(64, len(failed["payload"]["content_sha256"]))
        self.assertTrue(any(
            event.get("event_type")
            == (
                "debug.delegate.output_contract_repair_length_"
                "continuation.failed"
            )
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "run.completed" for event in events
        ))
        self.assertEqual("error", events[-1]["type"])

    async def test_output_contract_repair_raw_continuation_is_atomic(self):
        invalid_result = (
            "rejected draft\n"
            "<tool_call><arguments>{}</arguments></tool_call>"
        )
        repair_prefix = "# Buffered repair\nClean evidence prefix with provenance."
        contaminated_suffix = (
            "Conclusion starts here.\n"
            "<tool_call><name>execute_code</name><arguments>"
            '{"code":"print(2)"}</arguments></tool_call>'
        )

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._stop_lines(invalid_result),
                self._length_lines(repair_prefix),
                self._stop_lines(contaminated_suffix),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=5,
            delegated=True,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_not_awaited()
        emitted = [
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        ]
        self.assertNotIn(repair_prefix, emitted)
        self.assertNotIn(contaminated_suffix, emitted)
        failed = next(
            event for event in events
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "delegated_output_contract_repair_continuation_invalid",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("raw_pseudo_tool_protocol", failed["payload"]["reason"])
        self.assertGreater(failed["payload"]["raw_protocol_count"], 0)
        self.assertTrue(failed["payload"]["transactional_buffering"])
        self.assertFalse(any(
            event.get("event_type") == "run.completed" for event in events
        ))
        self.assertEqual("error", events[-1]["type"])

    async def test_primary_chat_does_not_enter_delegated_contract_repair(self):
        content = (
            "Documentation example: "
            "<tool_call><arguments>{\"query\":\"example\"}</arguments></tool_call>"
        )
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._stop_lines(content),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=3,
            delegated=False,
        )

        # This fixture deliberately requires read_file, so the ordinary
        # direct-required-tool gate owns the second turn.  The raw protocol
        # example must not replace that primary-chat policy with the
        # delegated tools-closed output repair.
        self.assertEqual(
            ["stream", "stream"],
            [request["kind"] for request in requests],
        )
        self.assertEqual(1, len(requests[1]["body"].get("tools") or []))
        self.assertEqual("required", requests[1]["body"].get("tool_choice"))
        dispatch_mock.assert_not_awaited()
        self.assertTrue(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "direct_required_tool"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_output_contract_repair"
            for event in events
        ))

    async def test_delegated_repair_failure_before_dispatch_remains_terminal(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="still-corrupt"),
                self._partial_corrupt_lines(content="third-corrupt"),
                self._partial_corrupt_lines(content="fourth-corrupt"),
            ],
            max_iterations=5,
            delegated=True,
        )

        self.assertEqual(
            [
                "stream", "stream", "fallback", "stream", "fallback",
                "stream", "fallback",
            ],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_recovery_exhausted",
            failed["payload"]["finish_reason"],
        )

    async def test_delegated_preflight_reject_does_not_enable_synthesis(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("outside-compiled-closure.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                self._partial_corrupt_lines(content="third-corrupt"),
                self._partial_corrupt_lines(content="fourth-corrupt"),
            ],
            max_iterations=5,
            delegated=True,
            delegated_resource_boundary=True,
            allowed_read_paths=[],
        )

        self.assertEqual(
            [
                "stream", "stream", "stream", "fallback",
                "stream", "fallback", "stream", "fallback",
            ],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_not_awaited()
        rejected_result = next(
            event for event in events
            if event.get("event_type") == "tool.failed"
        )
        self.assertIs(
            False,
            rejected_result["payload"]["actual_dispatch_attempted"],
        )
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_recovery_exhausted",
            failed["payload"]["finish_reason"],
        )

    async def test_post_dispatch_synthesis_length_continues_once_without_tools(self):
        truncated_recovery = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": "partial delegated synthesis",
                        "reasoning_content": "private recovery reasoning",
                    },
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                truncated_recovery,
                self._stop_lines(
                    "\nConclusion: retained evidence is explicitly degraded "
                    "with provenance and no unsupported facts."
                ),
            ],
            max_iterations=6,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertNotIn("tools", requests[4]["body"])
        self.assertNotIn("tools", requests[5]["body"])
        self.assertFalse(any(
            event.get("type") == "reasoning_delta"
            and "private recovery reasoning" in str(event.get("content"))
            for event in events
        ))
        recovery_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
        ]
        self.assertEqual(1, len(recovery_gates))
        length_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_length_continuation"
        ]
        self.assertEqual(1, len(length_gates))
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            in {"delegate_output_contract_repair", "delegate_result_footer_repair"}
            for event in events
        ))
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertIn("partial delegated synthesis", visible)
        self.assertIn("Conclusion: retained evidence", visible)
        terminal = [
            event for event in events
            if event.get("event_type") == "run.completed"
        ][-1]
        self.assertEqual(
            "post_dispatch_stream_recovery_synthesis",
            terminal["payload"]["terminal_reason"],
        )
        self.assertEqual("done", events[-1]["type"])

    async def test_reasoning_recovery_clean_partial_interrupt_continues_once(self):
        """A clean large prefix from the sole reasoning recovery is retained."""
        prefix_marker = "REASONING-RECOVERY-CLEAN-PREFIX"
        retained_prefix = prefix_marker + (
            " evidence with bounded provenance. " * 900
        )
        retained_prefix = retained_prefix[:26_977]
        self.assertEqual(26_977, len(retained_prefix))
        hidden_reasoning = "abandoned-initial-hidden-reasoning"
        initial_reasoning_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": hidden_reasoning},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial reasoning timeout"),
        ]
        clean_partial_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": retained_prefix},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture clean partial timeout"),
        ]
        unique_suffix = (
            "Conclusion: retained evidence is explicitly WARN/degraded.\n"
            "Provenance and unavailable gaps remain explicit."
        )

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                initial_reasoning_interrupt,
                clean_partial_interrupt,
                # Exercise full-prefix replay overlap removal on completion.
                self._stop_lines(retained_prefix + unique_suffix),
            ],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertIn("tools", requests[0]["body"])
        self.assertIn("tools", requests[1]["body"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[1]["body"].get("chat_template_kwargs"),
        )
        self.assertNotIn("tools", requests[2]["body"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[2]["body"].get("chat_template_kwargs"),
        )
        continuation_history = json.dumps(
            requests[2]["body"]["messages"],
            ensure_ascii=False,
        )
        self.assertEqual(1, continuation_history.count(prefix_marker))
        self.assertIn("Do not repeat, summarize", continuation_history)
        self.assertNotIn(hidden_reasoning, continuation_history)
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_reasoning_only_stream_recovery"
            for event in events
        ))
        clean_gate = next(
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_clean_visible_stream_continuation"
        )
        self.assertEqual(
            "reasoning_only_stream_recovery",
            clean_gate["payload"]["origin"],
        )
        self.assertEqual(26_977, clean_gate["payload"]["content_chars"])
        self.assertEqual(0, clean_gate["payload"]["reasoning_chars_discarded"])
        self.assertEqual(0, clean_gate["payload"]["dispatched_tool_result_count"])
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(retained_prefix + "\n" + unique_suffix, visible)
        self.assertEqual(1, visible.count(prefix_marker))
        terminal = [
            event for event in events
            if event.get("event_type") == "run.completed"
        ][-1]
        self.assertEqual(
            "delegated_visible_length_recovery",
            terminal["payload"]["terminal_reason"],
        )

    async def test_visible_completion_retries_one_byte_empty_transport(self):
        retained_prefix = (
            "RETAINED-BEFORE-EMPTY-TRANSPORT\n"
            + "verified provenance " * 40
        )
        initial_reasoning_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded reasoning"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial timeout"),
        ]
        clean_prefix_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": retained_prefix},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture prefix timeout"),
        ]
        empty_transport = [
            asyncio.TimeoutError("fixture byte-empty completion timeout"),
        ]
        suffix = (
            "Conclusion: retained evidence remains WARN/degraded.\n"
            "Provenance and blockers are explicit."
        )

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                initial_reasoning_interrupt,
                clean_prefix_interrupt,
                empty_transport,
                self._stop_lines(suffix),
            ],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(4, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertEqual(requests[2]["body"], requests[3]["body"])
        self.assertNotIn("tools", requests[2]["body"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[2]["body"].get("chat_template_kwargs"),
        )
        empty_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_terminal_empty_transport_retry"
        ]
        self.assertEqual(1, len(empty_gates))
        self.assertEqual(1, empty_gates[0]["payload"]["max_retries"])
        started = [
            event for event in events
            if event.get("event_type") == "debug.iteration.started"
        ]
        self.assertEqual(3, len(started))
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(retained_prefix + "\n" + suffix, visible)
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_visible_completion_second_byte_empty_transport_is_terminal(self):
        initial_reasoning_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded reasoning"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial timeout"),
        ]
        clean_prefix_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "RETAINED-ONCE"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture prefix timeout"),
        ]
        empty_transport = [asyncio.TimeoutError("fixture byte-empty timeout")]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                initial_reasoning_interrupt,
                clean_prefix_interrupt,
                empty_transport,
                empty_transport,
                self._stop_lines("must not be requested"),
            ],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(4, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertEqual(requests[2]["body"], requests[3]["body"])
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_terminal_empty_transport_retry"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(0, failed["payload"]["content_chars_discarded"])
        self.assertEqual(0, failed["payload"]["stream_fragment_count"])
        self.assertEqual("error", events[-1]["type"])

    async def test_scrubbed_raw_bytes_are_not_treated_as_empty_transport(self):
        initial_reasoning_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded reasoning"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial timeout"),
        ]
        clean_prefix_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "RETAINED-BEFORE-SCRUBBED-BYTES"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture prefix timeout"),
        ]
        scrubbed_but_nonempty = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "<think>"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture timeout after raw think tag"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                initial_reasoning_interrupt,
                clean_prefix_interrupt,
                scrubbed_but_nonempty,
                self._stop_lines("must not be requested"),
            ],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("payload", {}).get("gate")
            == "delegate_terminal_empty_transport_retry"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(0, failed["payload"]["content_chars_discarded"])
        self.assertEqual(7, failed["payload"]["raw_content_chars_received"])
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("error", events[-1]["type"])

    async def test_reasoning_recovery_clean_continuation_second_interrupt_is_terminal(self):
        retained_prefix = (
            "RETAINED-RECOVERY-PREFIX\n" + "verified provenance " * 40
        )
        abandoned_suffix = "ABANDONED-SECOND-INTERRUPT-SUFFIX"

        def interrupted(*, content="", reasoning=""):
            delta = {}
            if content:
                delta["content"] = content
            if reasoning:
                delta["reasoning_content"] = reasoning
            return [
                "data: " + json.dumps({
                    "choices": [{"delta": delta, "finish_reason": None}],
                }),
                asyncio.TimeoutError("fixture interrupted recovery"),
            ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                interrupted(reasoning="discarded-initial-reasoning"),
                interrupted(content=retained_prefix),
                interrupted(content=abandoned_suffix),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=4,
            delegated=True,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_clean_visible_stream_continuation"
            for event in events
        ))
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(retained_prefix + "\n", visible)
        self.assertNotIn(abandoned_suffix, visible)
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertTrue(failed["payload"]["continuation"])
        self.assertEqual("error", events[-1]["type"])

    async def test_reasoning_recovery_interrupted_tool_fragment_is_terminal(self):
        initial = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded reasoning"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial reasoning timeout"),
        ]
        malformed_fragment = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": "visible-but-protocol-tainted-prefix",
                        "tool_calls": [{
                            "index": 0,
                            "id": "malformed-recovery-call",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":',
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture malformed fragment timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [initial, malformed_fragment, self._stop_lines("must not run")],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(2, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_clean_visible_stream_continuation"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(1, failed["payload"]["stream_fragment_count"])
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )

    async def test_reasoning_recovery_interrupted_raw_protocol_is_terminal(self):
        raw_prefix = (
            "Clean-looking evidence prefix\n"
            '<tool_call><name>read_file</name><arguments>{"filepath":"x"}'
        )
        initial = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded reasoning"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial reasoning timeout"),
        ]
        raw_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": raw_prefix},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture raw protocol timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [initial, raw_interrupt, self._stop_lines("must not run")],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(2, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_clean_visible_stream_continuation"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(0, failed["payload"]["recovery_count"])

    async def test_reasoning_recovery_mixed_visible_and_reasoning_is_terminal(self):
        initial = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded reasoning"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial reasoning timeout"),
        ]
        mixed_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": "visible prefix mixed with new reasoning",
                        "reasoning_content": "new mixed hidden reasoning",
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture mixed stream timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [initial, mixed_interrupt, self._stop_lines("must not run")],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(2, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_clean_visible_stream_continuation"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertGreater(failed["payload"]["reasoning_chars_discarded"], 0)
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )

    async def test_reasoning_recovery_clean_partial_without_budget_is_terminal(self):
        retained_prefix = "NO-BUDGET-RECOVERY-PREFIX " + "evidence " * 40
        initial = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded reasoning"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial reasoning timeout"),
        ]
        clean_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": retained_prefix},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture no-budget partial timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [initial, clean_interrupt],
            max_iterations=2,
            delegated=True,
        )

        self.assertEqual(2, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_clean_visible_stream_continuation"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertFalse(failed["payload"]["continuation"])

    async def test_reasoning_recovery_exact_prefix_replay_is_no_progress(self):
        retained_prefix = (
            "EXACT-REPLAY-RECOVERY-PREFIX\n" + "bounded evidence " * 40
        )
        initial = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded reasoning"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial reasoning timeout"),
        ]
        clean_interrupt = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": retained_prefix},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture clean partial timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [initial, clean_interrupt, self._stop_lines(retained_prefix)],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_not_awaited()
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(retained_prefix + "\n", visible)
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "empty_unique_visible_suffix",
            failed["payload"]["reason"],
        )
        self.assertEqual(0, failed["payload"]["unique_visible_suffix_chars"])
        self.assertEqual("error", events[-1]["type"])

    async def test_post_dispatch_synthesis_partial_interrupt_continues_once(self):
        prefix_marker = "POST-DISPATCH-INTERRUPTED-PREFIX"
        retained_prefix = (
            "# Evidence\n"
            + prefix_marker
            + "\n"
            + ("Verified evidence with explicit provenance. " * 8)
        )
        typed_footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "degraded",
                "reason": "unavailable in retained evidence",
                "provenance": "evidence.md and attempted source/fallback",
            },
        })
        unique_suffix = (
            "Conclusion: retained evidence is WARN/degraded.\n"
            "Provenance: evidence.md.\n"
            "Gap: unavailable evidence remains explicit.\n"
            + typed_footer
        )
        reasoning_length = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-analysis"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        interrupted_synthesis = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": retained_prefix,
                        "reasoning_content": (
                            "INTERRUPTED-HIDDEN-REASONING-MUST-NOT-LEAK"
                        ),
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture synthesis timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_length,
                interrupted_synthesis,
                # Exercise overlap removal: the provider repeats the complete
                # retained prefix, but the outer stream must see it only once.
                self._stop_lines(retained_prefix + unique_suffix),
            ],
            max_iterations=5,
            delegated=True,
            required_result_fields=["study_title"],
        )

        self.assertEqual(
            ["stream", "stream", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        for request in requests[2:]:
            self.assertNotIn("tools", request["body"])
            self.assertEqual(
                {"enable_thinking": False},
                request["body"].get("chat_template_kwargs"),
            )
        continuation_history = json.dumps(
            requests[3]["body"]["messages"],
            ensure_ascii=False,
        )
        self.assertEqual(1, continuation_history.count(prefix_marker))
        self.assertIn("emit only the missing conclusion/status", continuation_history)
        self.assertIn("RESULT_FIELDS_JSON", continuation_history)
        self.assertNotIn(
            "INTERRUPTED-HIDDEN-REASONING-MUST-NOT-LEAK",
            continuation_history,
        )
        gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_length_continuation"
        ]
        self.assertEqual(1, len(gates))
        self.assertTrue(gates[0]["payload"]["stream_interrupted"])
        self.assertEqual(
            "TimeoutError",
            gates[0]["payload"]["exception_kind"],
        )
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(1, visible.count(prefix_marker))
        self.assertEqual(1, visible.count("RESULT_FIELDS_JSON:"))
        self.assertEqual(retained_prefix + "\n" + unique_suffix, visible)
        self.assertFalse(any(
            event.get("type") == "reasoning_delta"
            and "INTERRUPTED-HIDDEN-REASONING-MUST-NOT-LEAK"
            in str(event.get("content") or "")
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        terminal = [
            event for event in events
            if event.get("event_type") == "run.completed"
        ][-1]
        self.assertEqual(
            "post_dispatch_stream_recovery_synthesis",
            terminal["payload"]["terminal_reason"],
        )

    async def test_reserved_final_synthesis_partial_interrupt_continues_once(self):
        prefix_marker = "RESERVED-SYNTHESIS-INTERRUPTED-PREFIX"
        retained_prefix = (
            "# Evidence\n"
            + prefix_marker
            + "\nStatus: WARN/degraded. Provenance: prior tool results.\n"
        )
        interrupted_synthesis = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": retained_prefix},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture reserved synthesis timeout"),
        ]
        completion = (
            "Conclusion: the bounded evidence remains degraded.\n"
            "Gap: unavailable secondary evidence.\n"
            "RESULT_FIELDS_JSON: "
            + json.dumps({
                "evidence": {
                    "status": "degraded",
                    "reason": "secondary evidence unavailable",
                    "provenance": "prior tool results",
                },
            })
        )

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence-1.md"),
                self._valid_tool_lines("evidence-2.md"),
                interrupted_synthesis,
                self._stop_lines(completion),
            ],
            max_iterations=4,
            delegated=True,
            required_result_fields=["evidence"],
        )

        self.assertEqual(4, len(requests))
        self.assertEqual(2, dispatch_mock.await_count)
        for request in requests[2:]:
            self.assertNotIn("tools", request["body"])
            self.assertEqual(
                {"enable_thinking": False},
                request["body"].get("chat_template_kwargs"),
            )
        continuation_history = json.dumps(
            requests[3]["body"]["messages"], ensure_ascii=False
        )
        self.assertEqual(1, continuation_history.count(prefix_marker))
        gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_synthesis_length_continuation"
        ]
        self.assertEqual(1, len(gates))
        self.assertEqual(
            "reserved_final_synthesis", gates[0]["payload"]["origin"]
        )
        self.assertTrue(gates[0]["payload"]["stream_interrupted"])
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(1, visible.count(prefix_marker))
        self.assertEqual(retained_prefix + "\n" + completion, visible)
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_clean_length_after_reasoning_recovery_and_dispatch_continues(self):
        private_reasoning = "INITIAL-PRIVATE-REASONING-MUST-NOT-LEAK"
        reasoning_only_interruption = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": private_reasoning},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture initial reasoning timeout"),
        ]
        retained_prefix = (
            "# Recovered evidence\n"
            "Status: WARN/degraded. Provenance: required capability result.\n"
            "The bounded conclusion was truncated"
        )
        visible_length = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": retained_prefix},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        completion = (
            "Conclusion: retain WARN because one source remains unavailable.\n"
            "RESULT_FIELDS_JSON: "
            + json.dumps({
                "evidence": {
                    "status": "degraded",
                    "reason": "one source unavailable",
                    "provenance": "required capability result",
                },
            })
        )

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                reasoning_only_interruption,
                self._valid_tool_lines("required-evidence.md"),
                visible_length,
                self._stop_lines(completion),
            ],
            max_iterations=6,
            delegated=True,
            required_result_fields=["evidence"],
            required_capability_tools=["read_file"],
        )

        self.assertEqual(4, len(requests))
        dispatch_mock.assert_awaited_once()
        self.assertEqual("required", requests[1]["body"].get("tool_choice"))
        self.assertIn("tools", requests[2]["body"])
        self.assertNotIn("tools", requests[3]["body"])
        gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
        ]
        self.assertEqual(1, len(gates))
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(retained_prefix + "\n" + completion, visible)
        self.assertNotIn(private_reasoning, json.dumps(
            requests[-1]["body"]["messages"], ensure_ascii=False
        ))
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_native_schema_visible_length_uses_same_footer_contract(self):
        retained_prefix = (
            "# Evidence\n"
            "The calculator result was retained with source provenance."
        )
        schema = {
            "sum": {"type": "number"},
            "rows": {
                "type": "array",
                "items": {"type": "integer"},
            },
        }
        native_footer = (
            "RESULT_FIELDS_JSON: "
            + json.dumps({"sum": 7.5, "rows": [1, 2, 3]})
        )

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._length_lines(retained_prefix),
                self._stop_lines(
                    "Conclusion: the retained native values are supported."
                ),
                self._structured_tool_lines(
                    "submit_result_fields",
                    {"sum": 7.5, "rows": [1, 2, 3]},
                ),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=5,
            delegated=True,
            required_result_fields=["sum", "rows"],
            required_result_schema=schema,
        )

        self.assertEqual(4, len(requests))
        dispatch_mock.assert_awaited_once()
        self.assertNotIn("tools", requests[2]["body"])
        self.assertEqual(
            "submit_result_fields",
            requests[3]["body"]["tools"][0]["function"]["name"],
        )
        repair_prompt = requests[3]["body"]["messages"][-1]["content"]
        self.assertIn("required_result_schema", repair_prompt)
        self.assertIn('"sum":{"type":"number"}', repair_prompt)
        self.assertEqual(0, requests[3]["body"]["temperature"])
        self.assertEqual("required", requests[3]["body"]["tool_choice"])
        visible_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
        ]
        self.assertEqual(1, len(visible_gates))
        visible_gate = visible_gates[0]
        self.assertEqual(2, visible_gate["payload"]["required_field_count"])
        footer_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_result_footer_repair"
        ]
        self.assertEqual(1, len(footer_gates))
        footer_gate = footer_gates[0]
        self.assertEqual(2, footer_gate["payload"]["required_field_count"])
        dispatch_mock.assert_awaited_once()
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(1, visible.count("RESULT_FIELDS_JSON:"))
        footer_value = json.loads(
            visible.rsplit("RESULT_FIELDS_JSON: ", 1)[1]
        )
        self.assertEqual({"sum": 7.5, "rows": [1, 2, 3]}, footer_value)
        completed = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.completed"
        )
        self.assertTrue(completed["payload"]["footer_valid"])
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_verified_preloaded_typed_tools_closed_length_recovers_across_domains(self):
        cases = (
            (
                "software_release",
                "Assess the supplied release manifest without external calls.",
                ["release_status", "blocking_issue"],
            ),
            (
                "supply_chain",
                "Reconcile the preloaded shipment records without external calls.",
                ["shipment_status", "delay_reason"],
            ),
            (
                "astronomy",
                "Summarize the preloaded telescope observations without external calls.",
                ["observation_status", "data_gap"],
            ),
        )
        for domain, task_text, fields in cases:
            prefix_marker = f"{domain.upper()}-VERIFIED-PRELOAD-PREFIX"
            retained_prefix = (
                f"# {domain} result\n{prefix_marker}\n"
                "Status and provenance are derived only from the preloaded "
                "deterministic inputs; the bounded conclusion was truncated"
            )
            footer_values = {
                fields[0]: {
                    "status": "present",
                    "value_summary": f"{domain} bounded status",
                    "provenance": "verified deterministic preload",
                },
                fields[1]: {
                    "status": "degraded",
                    "reason": f"{domain} explicit gap",
                    "provenance": "verified deterministic preload",
                },
            }
            completion = (
                "Conclusion: preserve the bounded status and explicit gap.\n"
                "RESULT_FIELDS_JSON: "
                + json.dumps(footer_values, separators=(",", ":"))
            )

            with self.subTest(domain=domain):
                requests, dispatch_mock, events = await self._run_stream_sequence(
                    [
                        self._length_lines(retained_prefix),
                        self._stop_lines(completion),
                        self._stop_lines("must not be requested"),
                    ],
                    max_iterations=4,
                    delegated=True,
                    enabled_tool=None,
                    task_text=task_text,
                    required_result_fields=fields,
                    verified_preloaded_input_receipt=(
                        self._verified_preload_receipt(
                            source_count=2,
                            kind_counts={"read_file": 1, "skill_view": 1},
                        )
                    ),
                )

            self.assertEqual(2, len(requests))
            dispatch_mock.assert_not_awaited()
            for request in requests:
                self.assertNotIn("tools", request["body"])
                self.assertEqual(
                    {"enable_thinking": False},
                    request["body"].get("chat_template_kwargs"),
                )
            continuation_history = json.dumps(
                requests[1]["body"]["messages"], ensure_ascii=False
            )
            self.assertEqual(1, continuation_history.count(prefix_marker))
            self.assertIn("Do not repeat", continuation_history)
            self.assertIn("do not add new facts", continuation_history)
            self.assertIn("no tools are available", continuation_history)
            gates = [
                event for event in events
                if event.get("event_type") == "debug.gate.continuation"
                and event.get("payload", {}).get("gate")
                == "delegate_visible_length_recovery"
            ]
            self.assertEqual(1, len(gates))
            gate = gates[0]["payload"]
            self.assertEqual(
                "verified_preloaded_tools_closed_synthesis",
                gate["origin"],
            )
            self.assertEqual(
                "verified_preloaded_tools_closed_synthesis",
                gate["evidence_origin"],
            )
            self.assertTrue(gate["verified_preloaded_input_receipt"])
            self.assertEqual(2, gate["verified_preloaded_source_count"])
            self.assertEqual(0, gate["dispatched_tool_result_count"])
            self.assertTrue(gate["actual_tool_schema_surface_empty"])
            # Debug carries only shape/provenance metadata, never report text.
            self.assertNotIn(prefix_marker, json.dumps(gate))
            visible = "".join(
                str(event.get("content") or "")
                for event in events
                if event.get("type") == "delta"
            )
            self.assertEqual(retained_prefix + "\n" + completion, visible)
            self.assertFalse(any(
                event.get("event_type") == "run.failed" for event in events
            ))
            self.assertEqual("done", events[-1]["type"])

    async def test_typed_tools_closed_length_requires_bound_complete_preload_receipt(self):
        valid = self._verified_preload_receipt()
        partial = dict(valid)
        partial.pop("complete")
        wrong_binding = dict(valid)
        wrong_binding["session_id"] = "different-session"
        wrong_run = dict(valid)
        wrong_run["run_id"] = "sibling-child-run"
        wrong_user = dict(valid)
        wrong_user["user_id"] = "different-user"
        wrong_workspace = dict(valid)
        wrong_workspace["workspace_scope"] = "isolated-sibling-workspace"
        mismatched_counts = dict(valid)
        mismatched_counts["kind_counts"] = {"skill_view": 2}
        bool_version = dict(valid)
        bool_version["version"] = True
        malformed_digest = dict(valid)
        malformed_digest["aggregate_sha256"] = "not-a-sha256"
        invented_kind = dict(valid)
        invented_kind["kind_counts"] = {"invented_loader": 1}
        cases = (
            ("missing", None),
            ("partial", partial),
            ("wrong_binding", wrong_binding),
            ("wrong_run", wrong_run),
            ("wrong_user", wrong_user),
            ("wrong_workspace", wrong_workspace),
            ("mismatched_counts", mismatched_counts),
            ("bool_version", bool_version),
            ("malformed_digest", malformed_digest),
            ("invented_kind", invented_kind),
        )
        for label, receipt in cases:
            with self.subTest(receipt=label):
                requests, dispatch_mock, events = await self._run_stream_sequence(
                    [
                        self._length_lines(
                            f"{label}: clean typed prefix without verified inputs"
                        ),
                        self._stop_lines("must not be requested"),
                    ],
                    max_iterations=4,
                    delegated=True,
                    enabled_tool=None,
                    required_result_fields=["status"],
                    verified_preloaded_input_receipt=receipt,
                )

            self.assertEqual(1, len(requests))
            dispatch_mock.assert_not_awaited()
            self.assertFalse(any(
                event.get("event_type") == "debug.gate.continuation"
                and event.get("payload", {}).get("gate")
                == "delegate_visible_length_recovery"
                for event in events
            ))
            failed = next(
                event for event in events
                if event.get("event_type") == "run.failed"
            )
            self.assertEqual("length", failed["payload"]["finish_reason"])

    async def test_untyped_tools_closed_never_uses_preload_length_receipt(self):
        cases = (
            {
                "label": "untyped_tools_closed",
                "required_result_fields": None,
                "enabled_tool": None,
            },
        )
        for case in cases:
            with self.subTest(case=case["label"]):
                requests, dispatch_mock, events = await self._run_stream_sequence(
                    [
                        self._length_lines(
                            case["label"] + ": clean visible prefix"
                        ),
                        self._stop_lines("must not be requested"),
                    ],
                    max_iterations=4,
                    delegated=True,
                    enabled_tool=case["enabled_tool"],
                    required_result_fields=case["required_result_fields"],
                    verified_preloaded_input_receipt=(
                        self._verified_preload_receipt()
                    ),
                )

            self.assertEqual(1, len(requests))
            dispatch_mock.assert_not_awaited()
            self.assertFalse(any(
                event.get("event_type") == "debug.gate.continuation"
                and event.get("payload", {}).get("gate")
                == "delegate_visible_length_recovery"
                for event in events
            ))

    async def test_verified_preloaded_typed_open_optional_tools_length_closes_once(self):
        prefix = (
            "# Cross-domain evidence\n"
            "The verified preload supports a substantive typed result, but "
            "the provider truncated the clean visible body before its footer."
        )
        completion = (
            "Conclusion: the bounded result is complete.\n"
            'RESULT_FIELDS_JSON: {"status":"complete"}'
        )
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [self._length_lines(prefix), self._stop_lines(completion)],
            max_iterations=3,
            delegated=True,
            enabled_tool="read_file",
            task_text="Synthesize the verified preloaded evidence.",
            required_result_fields=["status"],
            required_result_schema={"status": {"type": "string"}},
            verified_preloaded_input_receipt=self._verified_preload_receipt(),
        )

        self.assertEqual(2, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertEqual(
            "read_file", requests[0]["body"]["tools"][0]["function"]["name"]
        )
        self.assertNotIn("tools", requests[1]["body"])
        for request in requests:
            self.assertEqual(
                {"enable_thinking": False},
                request["body"].get("chat_template_kwargs"),
            )
        gate = next(
            event["payload"] for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
        )
        self.assertEqual(
            "verified_preloaded_optional_tools_synthesis", gate["origin"]
        )
        self.assertEqual(
            "verified_preloaded_optional_tools_synthesis",
            gate["evidence_origin"],
        )
        self.assertFalse(gate["actual_tool_schema_surface_empty"])
        self.assertEqual(0, gate["dispatched_tool_result_count"])
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(prefix + "\n" + completion, visible)
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))

    async def test_verified_preloaded_typed_open_tool_turn_disables_private_thinking_only(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._stop_lines(
                    "Typed synthesis remains observable while the optional "
                    "tool surface stays open.\n"
                    'RESULT_FIELDS_JSON: {"status":"complete"}'
                ),
            ],
            max_iterations=2,
            delegated=True,
            enabled_tool="read_file",
            task_text="Synthesize the verified preloaded release evidence.",
            required_result_fields=["status"],
            required_result_schema={"status": {"type": "string"}},
            verified_preloaded_input_receipt=self._verified_preload_receipt(),
        )

        self.assertEqual(1, len(requests))
        dispatch_mock.assert_not_awaited()
        request = requests[0]["body"]
        self.assertEqual("read_file", request["tools"][0]["function"]["name"])
        self.assertNotIn("tool_choice", request)
        self.assertEqual(
            {"enable_thinking": False},
            request.get("chat_template_kwargs"),
        )
        debug_request = next(
            event for event in events
            if event.get("event_type") == "debug.llm.request"
        )
        self.assertTrue(
            debug_request["payload"][
                "delegate_verified_preloaded_typed_turn"
            ]
        )
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))

    async def test_verified_preloaded_untyped_open_tool_turn_keeps_provider_thinking_default(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [self._stop_lines("Untyped delegated synthesis.")],
            max_iterations=2,
            delegated=True,
            enabled_tool="read_file",
            task_text="Summarize the preloaded context without a typed contract.",
            verified_preloaded_input_receipt=self._verified_preload_receipt(),
        )

        self.assertEqual(1, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertNotIn("chat_template_kwargs", requests[0]["body"])
        debug_request = next(
            event for event in events
            if event.get("event_type") == "debug.llm.request"
        )
        self.assertFalse(
            debug_request["payload"][
                "delegate_verified_preloaded_typed_turn"
            ]
        )
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))

    async def test_verified_preloaded_length_raw_protocol_is_rejected(self):
        raw_prefix = (
            "# Build result\nStatus is bounded.\n"
            "<tool_call><name>execute_code</name><arguments>{}</arguments>"
            "</tool_call>"
        )
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._length_lines(raw_prefix),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=4,
            delegated=True,
            enabled_tool=None,
            required_result_fields=["build_status"],
            verified_preloaded_input_receipt=self._verified_preload_receipt(),
        )

        self.assertEqual(1, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertGreater(
            failed["payload"]["delegated_length_recovery_audit"][
                "raw_tool_protocol_count"
            ],
            0,
        )

    async def test_verified_preloaded_completion_anomaly_is_terminal_exactly_once(self):
        anomaly_batches = (
            (
                "second_length",
                self._length_lines("distinct incomplete suffix"),
                "delegated_visible_length_recovery_failed",
            ),
            (
                "empty_stop",
                self._stop_lines(""),
                "delegated_visible_length_recovery_failed",
            ),
            (
                "raw_protocol_stop",
                self._stop_lines(
                    "<tool_call><name>read_file</name><arguments>{}"
                    "</arguments></tool_call>"
                ),
                "delegated_visible_length_recovery_protocol_invalid",
            ),
        )
        for label, second_batch, expected_finish in anomaly_batches:
            with self.subTest(anomaly=label):
                requests, dispatch_mock, events = await self._run_stream_sequence(
                    [
                        self._length_lines(
                            f"{label}: retained verified preload prefix"
                        ),
                        second_batch,
                        self._stop_lines("must not be requested"),
                    ],
                    max_iterations=5,
                    delegated=True,
                    enabled_tool=None,
                    required_result_fields=["status"],
                    verified_preloaded_input_receipt=(
                        self._verified_preload_receipt()
                    ),
                )

            self.assertEqual(2, len(requests))
            dispatch_mock.assert_not_awaited()
            self.assertEqual(1, sum(
                event.get("event_type") == "debug.gate.continuation"
                and event.get("payload", {}).get("gate")
                == "delegate_visible_length_recovery"
                for event in events
            ))
            self.assertFalse(any(
                event.get("event_type") == "debug.gate.continuation"
                and event.get("payload", {}).get("gate") in {
                    "delegate_output_contract_repair",
                    "delegate_result_footer_repair",
                }
                for event in events
            ))
            failed = next(
                event for event in events
                if event.get("event_type") == "run.failed"
            )
            self.assertEqual(expected_finish, failed["payload"]["finish_reason"])
            self.assertEqual("error", events[-1]["type"])

    async def test_verified_preloaded_malformed_footer_uses_one_internal_submitter(self):
        retained_prefix = (
            "# Release evidence\nVERIFIED-PRELOAD-FOOTER-BRIDGE\n"
            "The release status and gap are bounded by the preloaded inputs."
        )
        completion_with_bad_footer = (
            "Conclusion: the release remains blocked with an explicit gap.\n"
            'RESULT_FIELDS_JSON: {"release_status":"blocked",'
        )
        canonical_values = {
            "release_status": {
                "status": "present",
                "value_summary": "blocked",
                "provenance": "verified deterministic preload",
            },
            "blocking_issue": {
                "status": "degraded",
                "reason": "preloaded dependency gap",
                "provenance": "verified deterministic preload",
            },
        }
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._length_lines(retained_prefix),
                self._stop_lines(completion_with_bad_footer),
                self._structured_tool_lines(
                    "submit_result_fields", canonical_values
                ),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=5,
            delegated=True,
            enabled_tool=None,
            required_result_fields=["release_status", "blocking_issue"],
            verified_preloaded_input_receipt=self._verified_preload_receipt(),
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_visible_length_recovery"
            for event in events
        ))
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_result_footer_repair"
            for event in events
        ))
        self.assertEqual(
            "submit_result_fields",
            requests[2]["body"]["tools"][0]["function"]["name"],
        )
        self.assertEqual("required", requests[2]["body"]["tool_choice"])
        self.assertEqual(0, requests[2]["body"]["temperature"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[2]["body"].get("chat_template_kwargs"),
        )
        self.assertFalse(any(
            event.get("event_type") == "tool.dispatch_started"
            for event in events
        ))
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(1, visible.count("RESULT_FIELDS_JSON:"))
        self.assertNotIn(
            'RESULT_FIELDS_JSON: {"release_status":"blocked",', visible
        )
        self.assertEqual(
            canonical_values,
            json.loads(visible.rsplit("RESULT_FIELDS_JSON: ", 1)[1]),
        )
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_post_dispatch_interrupt_continuation_second_interrupt_is_terminal(self):
        retained_prefix = (
            "# Evidence\nFIRST-RETAINED-PREFIX\n"
            + ("Verified evidence with provenance. " * 8)
        )
        second_partial = "SECOND-INTERRUPTED-SUFFIX-MUST-NOT-EMIT"
        reasoning_length = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-analysis"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]

        def interrupted(content):
            return [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {"content": content},
                        "finish_reason": None,
                    }],
                }),
                asyncio.TimeoutError("fixture synthesis timeout"),
            ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_length,
                interrupted(retained_prefix),
                interrupted(second_partial),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=6,
            delegated=True,
        )

        self.assertEqual(4, len(requests))
        dispatch_mock.assert_awaited_once()
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_length_continuation"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate") in {
                "delegate_output_contract_repair",
                "delegate_result_footer_repair",
                "delegate_visible_length_recovery",
            }
            for event in events
        ))
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(1, visible.count("FIRST-RETAINED-PREFIX"))
        self.assertNotIn(second_partial, visible)
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("error", events[-1]["type"])

    async def test_post_dispatch_interrupt_continuation_exact_prefix_replay_is_terminal(self):
        retained_prefix = (
            "# Evidence\nEXACT-REPLAY-RETAINED-PREFIX\n"
            + ("Verified evidence with provenance. " * 8)
        )
        reasoning_length = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-analysis"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]

        def interrupted(content):
            return [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {"content": content},
                        "finish_reason": None,
                    }],
                }),
                asyncio.TimeoutError("fixture synthesis timeout"),
            ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_length,
                interrupted(retained_prefix),
                # The continuation stops normally but contributes no unique
                # byte after deterministic overlap removal.
                self._stop_lines(retained_prefix),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=6,
            delegated=True,
        )

        self.assertEqual(4, len(requests))
        dispatch_mock.assert_awaited_once()
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(retained_prefix + "\n", visible)
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "empty_unique_visible_suffix",
            failed["payload"]["reason"],
        )
        self.assertEqual(0, failed["payload"]["unique_visible_suffix_chars"])
        self.assertFalse(any(
            event.get("event_type") == "run.completed" for event in events
        ))
        self.assertEqual("error", events[-1]["type"])

    async def test_post_dispatch_partial_interrupt_without_budget_is_terminal(self):
        retained_prefix = (
            "# Evidence\nNO-BUDGET-PREFIX\n"
            + ("Verified evidence with provenance. " * 8)
        )
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": retained_prefix},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture synthesis timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                interrupted,
            ],
            max_iterations=4,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_length_continuation"
            for event in events
        ))
        self.assertFalse(any(
            event.get("type") == "delta"
            and "NO-BUDGET-PREFIX" in str(event.get("content") or "")
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )

    async def test_ordinary_post_dispatch_mixed_interrupt_synthesizes_once(self):
        ordinary_partial = (
            "ORDINARY-OPEN-TOOLS-PARTIAL "
            + ("evidence narration " * 16)
        )
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": ordinary_partial,
                        "reasoning_content": "discarded mixed reasoning",
                        "tool_calls": [{
                            "index": 0,
                            "id": "incomplete-current-turn-call",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":',
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture ordinary timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                interrupted,
                self._stop_lines(
                    "status: WARN/degraded\nEvidence: evidence.md retained "
                    "with provenance and explicit gaps."
                ),
            ],
            max_iterations=5,
            delegated=True,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_awaited_once()
        self.assertIn("tools", requests[1]["body"])
        self.assertNotIn("tools", requests[2]["body"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[2]["body"].get("chat_template_kwargs"),
        )
        gate = next(
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
        )
        self.assertEqual(
            "provider_stream_interrupted_after_post_dispatch_generation",
            gate["payload"]["reason"],
        )
        self.assertGreater(gate["payload"]["content_chars_discarded"], 0)
        self.assertGreater(gate["payload"]["reasoning_chars_discarded"], 0)
        self.assertEqual(1, gate["payload"]["stream_fragment_count"])
        recovery_history = json.dumps(
            requests[2]["body"]["messages"],
            ensure_ascii=False,
        )
        self.assertNotIn(ordinary_partial, recovery_history)
        self.assertNotIn("discarded mixed reasoning", recovery_history)
        self.assertNotIn("incomplete-current-turn-call", recovery_history)
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_ordinary_predispatch_partial_interrupt_remains_terminal(self):
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": "ordinary partial without committed evidence",
                        "reasoning_content": "mixed reasoning",
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture predispatch timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [interrupted, self._stop_lines("must not be requested")],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(1, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(0, failed["payload"]["stream_fragment_count"])
        self.assertEqual("TimeoutError", failed["payload"]["exception_kind"])

    async def test_post_dispatch_interrupted_raw_protocol_prefix_is_terminal(self):
        raw_prefix = (
            "# Evidence\n"
            + ("Verified evidence with provenance. " * 8)
            + "\n<tool_call>write_file"
        )
        reasoning_length = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-analysis"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": raw_prefix},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture synthesis timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_length,
                interrupted,
                self._stop_lines("must not be requested"),
            ],
            max_iterations=5,
            delegated=True,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_awaited_once()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_length_continuation"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(0, failed["payload"]["continuation_count"])
        self.assertFalse(any(
            event.get("type") == "delta"
            and "<tool_call>" in str(event.get("content") or "")
            for event in events
        ))
        self.assertEqual("error", events[-1]["type"])

    async def test_post_dispatch_clean_prefix_unexpected_exception_continues_once(self):
        retained_prefix = (
            "# Evidence\nGENERIC-EXCEPTION-RETAINED-PREFIX\n"
            + ("Verified evidence with provenance. " * 8)
        )
        suffix = (
            "Conclusion: explicit WARN status.\n"
            "Provenance: evidence.md; gaps remain degraded."
        )
        reasoning_length = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-analysis"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": retained_prefix},
                    "finish_reason": None,
                }],
            }),
            ValueError("fixture unexpected stream failure"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_length,
                interrupted,
                self._stop_lines(suffix),
            ],
            max_iterations=5,
            delegated=True,
        )

        self.assertEqual(4, len(requests))
        dispatch_mock.assert_awaited_once()
        self.assertNotIn("tools", requests[-1]["body"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[-1]["body"].get("chat_template_kwargs"),
        )
        gate = next(
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_length_continuation"
        )
        self.assertEqual("ValueError", gate["payload"]["exception_kind"])
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(retained_prefix + "\n" + suffix, visible)
        self.assertEqual("done", events[-1]["type"])

    async def test_post_dispatch_interrupted_native_tool_fragment_is_terminal(self):
        clean_prefix = (
            "# Evidence\nNATIVE-FRAGMENT-PREFIX\n"
            + ("Verified evidence with provenance. " * 8)
        )
        reasoning_length = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-analysis"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": clean_prefix,
                        "tool_calls": [{
                            "index": 0,
                            "id": "forbidden-native-call",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":"evidence.md"}',
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture synthesis timeout"),
        ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_length,
                interrupted,
                self._stop_lines("must not be requested"),
            ],
            max_iterations=5,
            delegated=True,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_awaited_once()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_length_continuation"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(1, failed["payload"]["stream_fragment_count"])
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertFalse(any(
            event.get("type") == "delta"
            and "NATIVE-FRAGMENT-PREFIX" in str(event.get("content") or "")
            for event in events
        ))

    async def test_post_tool_reasoning_lengths_route_to_visible_closed_synthesis(self):
        def reasoning_length(secret):
            return [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {"reasoning_content": secret},
                        "finish_reason": "length",
                    }],
                }),
                "data: [DONE]",
            ]

        footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Bounded Study",
                "provenance": "retained tool evidence",
            },
        })
        final_result = (
            "status: WARN/degraded\n"
            "Evidence: retained tool evidence with explicit provenance.\n"
            "Gap: unavailable fields remain explicitly degraded.\n"
            + footer
        )
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_length("first-private-reasoning"),
                reasoning_length("second-private-reasoning"),
                self._stop_lines(final_result),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=5,
            delegated=True,
            required_result_fields=["study_title"],
        )

        self.assertEqual(
            ["stream", "stream", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertIn("tools", requests[0]["body"])
        self.assertIn("tools", requests[1]["body"])
        self.assertNotIn("chat_template_kwargs", requests[0]["body"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[1]["body"].get("chat_template_kwargs"),
        )
        for request in requests[2:]:
            self.assertNotIn("tools", request["body"])
            self.assertEqual(
                {"enable_thinking": False},
                request["body"].get("chat_template_kwargs"),
            )
        serialized_recovery_history = json.dumps(
            requests[-1]["body"]["messages"],
            ensure_ascii=False,
        )
        self.assertNotIn("first-private-reasoning", serialized_recovery_history)
        self.assertNotIn("second-private-reasoning", serialized_recovery_history)
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_reasoning_only_length_recovery"
            for event in events
        ))
        post_dispatch_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
        ]
        self.assertEqual(1, len(post_dispatch_gates))
        self.assertEqual(
            "provider_output_limit_after_post_dispatch_reasoning",
            post_dispatch_gates[0]["payload"]["reason"],
        )
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_reasoning_only_continuation"
            for event in events
        ))
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual(final_result, visible)
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_post_tool_reasoning_recovery_third_length_is_terminal(self):
        def reasoning_length(secret):
            return [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {"reasoning_content": secret},
                        "finish_reason": "length",
                    }],
                }),
                "data: [DONE]",
            ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_length("first-private-reasoning"),
                reasoning_length("second-private-reasoning"),
                reasoning_length("third-private-reasoning"),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=6,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        for request in requests[2:]:
            self.assertNotIn("tools", request["body"])
            self.assertEqual(
                {"enable_thinking": False},
                request["body"].get("chat_template_kwargs"),
            )
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
            for event in events
        ))
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_reasoning_only_continuation"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_post_dispatch_synthesis_failed",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("length", failed["payload"]["provider_finish_reason"])
        self.assertEqual("error", events[-1]["type"])

    async def test_terminal_audit_repairs_raw_protocol_and_missing_footer_once(self):
        reasoning_only = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "reasoning_content": "discarded-private-reasoning",
                    },
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        invalid_result = (
            "draft without its typed ledger\n"
            "<tool_call><name>web_search</name><arguments>"
            '{"query":"retry"}</arguments></tool_call>'
        )
        clean_fields = {
            "study_title": {
                "status": "degraded",
                "reason": "not present in retained evidence",
                "provenance": "attempted source/fallback",
            },
        }
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_only,
                self._stop_lines(invalid_result),
                self._structured_tool_lines(
                    "submit_result_fields",
                    clean_fields,
                ),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=6,
            delegated=True,
            required_result_fields=["study_title"],
        )

        self.assertEqual(
            ["stream", "stream", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertNotIn("tools", requests[2]["body"])
        self.assertEqual(
            "submit_result_fields",
            requests[3]["body"]["tools"][0]["function"]["name"],
        )
        self.assertEqual("required", requests[3]["body"]["tool_choice"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[3]["body"].get("chat_template_kwargs"),
        )
        repair_history = json.dumps(
            requests[-1]["body"]["messages"],
            ensure_ascii=False,
        )
        self.assertIn("bounded typed-result projector", repair_history)
        self.assertIn("evidence.md", repair_history)
        self.assertNotIn("draft without its typed ledger", repair_history)
        self.assertNotIn('<arguments>{"query":"retry"}', repair_history)
        repair_gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_result_footer_repair"
        ]
        self.assertEqual(1, len(repair_gates))
        self.assertEqual(
            "raw_pseudo_tool_protocol",
            repair_gates[0]["payload"]["reason"],
        )
        self.assertTrue(
            repair_gates[0]["payload"][
                "post_dispatch_terminal_contract_audit"
            ]
        )
        self.assertTrue(repair_gates[0]["payload"]["evidence_capsule"])
        self.assertFalse(repair_gates[0]["payload"]["raw_protocol_replayed"])
        repair_iterations = [
            event["payload"]
            for event in events
            if event.get("event_type") == "debug.iteration.started"
            and event.get("payload", {}).get(
                "delegate_result_footer_repair"
            )
        ]
        self.assertEqual(1, len(repair_iterations))
        self.assertTrue(
            repair_iterations[0][
                "delegate_result_footer_repair_"
                "replace_invalid_source_turn"
            ]
        )
        completed = next(
            event for event in events
            if event.get("event_type")
            == "debug.delegate.result_footer_repair.completed"
        )
        self.assertTrue(completed["payload"]["footer_valid"])
        terminal = [
            event for event in events
            if event.get("event_type") == "run.completed"
        ][-1]
        self.assertEqual(
            "delegated_result_footer_structured_repair",
            terminal["payload"]["terminal_reason"],
        )
        self.assertEqual("done", events[-1]["type"])

    async def test_terminal_audit_contract_repair_second_bad_stop_is_terminal(self):
        reasoning_only = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-reasoning"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        invalid_result = (
            "still invalid\n"
            "<tool_call><arguments>{}</arguments></tool_call>"
        )
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_only,
                self._stop_lines(invalid_result),
                self._stop_lines(invalid_result),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=6,
            delegated=True,
            required_result_fields=["study_title"],
        )

        self.assertEqual(
            ["stream", "stream", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            "submit_result_fields",
            requests[-1]["body"]["tools"][0]["function"]["name"],
        )
        self.assertEqual(
            {"enable_thinking": False},
            requests[-1]["body"].get("chat_template_kwargs"),
        )
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_result_footer_repair"
            for event in events
        ))
        terminal = [
            event for event in events
            if event.get("event_type") == "run.failed"
        ][-1]
        self.assertEqual(
            "delegated_result_footer_structured_repair_failed",
            terminal["payload"]["finish_reason"],
        )
        self.assertEqual("error", events[-1]["type"])

    async def test_terminal_audit_repairs_missing_footer_without_raw_protocol(self):
        reasoning_only = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-reasoning"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        missing_footer = (
            "status: WARN/degraded\n"
            "Evidence: retained tool evidence with explicit provenance.\n"
            "Gap: the declared typed field is unavailable."
        )
        repaired_fields = {
            "study_title": {
                "status": "degraded",
                "reason": "not present in retained evidence",
                "provenance": "attempted source/fallback",
            },
        }
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_only,
                self._stop_lines(missing_footer),
                self._structured_tool_lines(
                    "submit_result_fields",
                    repaired_fields,
                ),
            ],
            max_iterations=6,
            delegated=True,
            required_result_fields=["study_title"],
        )

        self.assertEqual(4, len(requests))
        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            "submit_result_fields",
            requests[-1]["body"]["tools"][0]["function"]["name"],
        )
        self.assertEqual(
            {"enable_thinking": False},
            requests[-1]["body"].get("chat_template_kwargs"),
        )
        repair_gate = next(
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_result_footer_repair"
        )
        self.assertEqual(
            "post_dispatch_typed_footer_invalid",
            repair_gate["payload"]["reason"],
        )
        self.assertTrue(
            repair_gate["payload"]["post_dispatch_terminal_contract_audit"]
        )

    async def test_terminal_audit_does_not_expand_exhausted_budget_for_repair(self):
        reasoning_only = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-reasoning"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        invalid_result = (
            "typed result still missing\n"
            "<tool_call><arguments>{}</arguments></tool_call>"
        )
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                reasoning_only,
                self._stop_lines(invalid_result),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=3,
            delegated=True,
            required_result_fields=["study_title"],
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_awaited_once()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_result_footer_repair"
            for event in events
        ))
        terminal = [
            event for event in events
            if event.get("event_type") == "run.completed"
        ][-1]
        self.assertEqual(
            "delegated_result_footer_repair_unavailable",
            terminal["payload"]["terminal_reason"],
        )
        self.assertEqual("done", events[-1]["type"])

    async def test_post_dispatch_reasoning_only_length_continues_once_without_tools(self):
        secret = "private-post-dispatch-reasoning-must-be-discarded"
        reasoning_only_recovery = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": secret},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        footer = "RESULT_FIELDS_JSON: " + json.dumps({
            "study_title": {
                "status": "present",
                "value_summary": "Bounded Study",
                "provenance": "retained registry evidence",
            },
        })
        final_result = (
            "status: WARN/degraded\n"
            "Evidence: retained registry evidence with explicit provenance.\n"
            "Gap: unavailable fields remain explicitly degraded.\n"
            + footer
        )
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                reasoning_only_recovery,
                self._stop_lines(final_result),
            ],
            max_iterations=7,
            delegated=True,
            required_result_fields=["study_title"],
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertNotIn("tools", requests[4]["body"])
        self.assertNotIn("tools", requests[5]["body"])
        self.assertNotIn(
            secret,
            json.dumps(requests[5]["body"]["messages"], ensure_ascii=False),
        )
        self.assertFalse(any(
            event.get("type") == "reasoning_delta"
            and secret in str(event.get("content") or "")
            for event in events
        ))
        gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_reasoning_only_continuation"
        ]
        self.assertEqual(1, len(gates))
        self.assertEqual(0, gates[0]["payload"]["content_chars"])
        self.assertGreater(gates[0]["payload"]["reasoning_chars_discarded"], 0)
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate") in {
                "delegate_reasoning_only_length_recovery",
                "delegate_visible_length_recovery",
                "delegate_output_contract_repair",
                "delegate_result_footer_repair",
            }
            for event in events
        ))
        visible = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertTrue(visible.endswith(final_result))
        self.assertNotIn(secret, visible)
        terminal = [
            event for event in events
            if event.get("event_type") == "run.completed"
        ][-1]
        self.assertEqual(
            "post_dispatch_stream_recovery_synthesis",
            terminal["payload"]["terminal_reason"],
        )
        self.assertEqual("done", events[-1]["type"])

    async def test_post_dispatch_reasoning_only_continuation_second_length_is_terminal(self):
        def reasoning_length(secret):
            return [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {"reasoning_content": secret},
                        "finish_reason": "length",
                    }],
                }),
                "data: [DONE]",
            ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                reasoning_length("first-private-reasoning"),
                reasoning_length("second-private-reasoning"),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=8,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertNotIn("tools", requests[-1]["body"])
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_reasoning_only_continuation"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate") in {
                "delegate_reasoning_only_length_recovery",
                "delegate_visible_length_recovery",
                "delegate_output_contract_repair",
                "delegate_result_footer_repair",
            }
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        failed_index = events.index(failed)
        usage_events = [
            (index, event)
            for index, event in enumerate(events)
            if event.get("type") == "usage"
        ]
        self.assertTrue(usage_events)
        usage_index, terminal_usage = usage_events[-1]
        self.assertLess(usage_index, failed_index)
        self.assertGreater(terminal_usage["total_tokens"], 0)
        self.assertGreater(failed["payload"]["usage"]["total_tokens"], 0)
        self.assertEqual(
            terminal_usage["total_tokens"],
            failed["payload"]["usage"]["total_tokens"],
        )
        self.assertEqual(
            "provider_tool_stream_post_dispatch_synthesis_failed",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("length", failed["payload"]["provider_finish_reason"])
        self.assertEqual("error", events[-1]["type"])

    async def test_post_dispatch_reasoning_only_continuation_empty_stop_is_terminal(self):
        secret = "discarded-reasoning-before-empty-stop"
        reasoning_only_recovery = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": secret},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                reasoning_only_recovery,
                self._stop_lines(""),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=8,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertEqual("evidence.md", dispatch_mock.await_args.args[1]["filepath"])
        self.assertNotIn("tools", requests[-1]["body"])
        self.assertNotIn(
            secret,
            json.dumps(requests[-1]["body"]["messages"], ensure_ascii=False),
        )
        self.assertNotIn(secret, json.dumps(events, ensure_ascii=False))
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_reasoning_only_continuation"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_post_dispatch_synthesis_failed",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("empty_visible_result", failed["payload"]["reason"])
        self.assertEqual("stop", failed["payload"]["provider_finish_reason"])
        self.assertEqual("error", events[-1]["type"])

    async def test_post_dispatch_reasoning_only_continuation_native_fragment_is_terminal(self):
        secret = "discarded-reasoning-before-native-fragment"
        reasoning_only_recovery = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": secret},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                reasoning_only_recovery,
                self._valid_tool_lines("must-not-dispatch.md"),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=8,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertEqual("evidence.md", dispatch_mock.await_args.args[1]["filepath"])
        self.assertNotIn("tools", requests[-1]["body"])
        self.assertNotIn(
            secret,
            json.dumps(requests[-1]["body"]["messages"], ensure_ascii=False),
        )
        self.assertNotIn(secret, json.dumps(events, ensure_ascii=False))
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_reasoning_only_continuation"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "debug.tool.stream.fallback.requested"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_post_dispatch_synthesis_failed",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("provider_emitted_tool_protocol", failed["payload"]["reason"])
        self.assertEqual(1, failed["payload"]["stream_fragment_count"])
        self.assertEqual("error", events[-1]["type"])

    async def test_post_dispatch_synthesis_second_length_fails_without_chain(self):
        def truncated(content):
            return [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {"content": content},
                        "finish_reason": "length",
                    }],
                }),
                "data: [DONE]",
            ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                truncated("first retained post-dispatch synthesis"),
                truncated("second incomplete continuation"),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=7,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertNotIn("tools", requests[-1]["body"])
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_synthesis_length_continuation"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            in {
                "delegate_visible_length_recovery",
                "delegate_output_contract_repair",
                "delegate_result_footer_repair",
            }
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_post_dispatch_synthesis_failed",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("length", failed["payload"]["provider_finish_reason"])
        self.assertEqual("error", events[-1]["type"])

    async def test_post_dispatch_length_continuation_raw_protocol_is_terminal(self):
        truncated_recovery = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "retained evidence prefix"},
                    "finish_reason": "length",
                }],
            }),
            "data: [DONE]",
        ]
        raw_completion = (
            "\n<tool_call><name>execute_code</name><arguments>"
            '{"code":"print(1)"}</arguments></tool_call>'
        )
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                truncated_recovery,
                self._stop_lines(raw_completion),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=7,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            in {"delegate_output_contract_repair", "delegate_result_footer_repair"}
            for event in events
        ))
        emitted = [
            event for event in events
            if event.get("event_type")
            == "debug.tool.stream.post_dispatch_synthesis.emitted"
        ][-1]
        self.assertEqual(1, emitted["payload"]["raw_protocol_count"])
        self.assertEqual(
            "post_dispatch_stream_recovery_synthesis",
            [
                event for event in events
                if event.get("event_type") == "run.completed"
            ][-1]["payload"]["terminal_reason"],
        )
        self.assertEqual("done", events[-1]["type"])

    async def test_post_dispatch_synthesis_native_tool_fragment_is_terminal(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                self._partial_corrupt_lines(),
                self._partial_corrupt_lines(content="repair-still-corrupt"),
                self._valid_tool_lines("must-not-dispatch.md"),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=7,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream", "fallback", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            "evidence.md",
            dispatch_mock.await_args.args[1]["filepath"],
        )
        self.assertFalse(any(
            event.get("event_type") == "debug.tool.stream.fallback.requested"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_post_dispatch_synthesis_failed",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("provider_emitted_tool_protocol", failed["payload"]["reason"])
        self.assertEqual("error", events[-1]["type"])

    async def test_reasoning_only_stream_interrupt_after_dispatch_synthesizes_once(self):
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "reasoning_content": "discarded-interrupt-reasoning",
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture stream timeout"),
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("evidence.md"),
                interrupted,
                self._stop_lines(
                    "status: WARN/degraded\nEvidence: evidence.md retained "
                    "with provenance and explicit gaps."
                ),
            ],
            max_iterations=5,
            delegated=True,
        )

        self.assertEqual(
            ["stream", "stream", "stream"],
            [request["kind"] for request in requests],
        )
        dispatch_mock.assert_awaited_once()
        self.assertNotIn("tools", requests[2]["body"])
        recovery_history = json.dumps(
            requests[2]["body"]["messages"],
            ensure_ascii=False,
        )
        self.assertIn("Earlier tool results", recovery_history)
        self.assertIn("evidence.md", recovery_history)
        self.assertNotIn("discarded-interrupt-reasoning", recovery_history)
        gates = [
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_post_dispatch_stream_synthesis"
        ]
        self.assertEqual(1, len(gates))
        self.assertEqual(
            "provider_stream_interrupted_after_reasoning",
            gates[0]["payload"]["reason"],
        )
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_predispatch_reasoning_only_stream_interrupt_recovers_once(self):
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-analysis"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture stream timeout"),
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [interrupted, self._stop_lines("bounded substantive result")],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(["stream", "stream"], [
            request["kind"] for request in requests
        ])
        dispatch_mock.assert_not_awaited()
        self.assertIn("tools", requests[1]["body"])
        self.assertNotIn("chat_template_kwargs", requests[0]["body"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[1]["body"].get("chat_template_kwargs"),
        )
        self.assertNotIn(
            "discarded-analysis",
            json.dumps(requests[1]["body"]["messages"], ensure_ascii=False),
        )
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_reasoning_only_stream_recovery"
            for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_required_capability_is_forced_once_across_reasoning_recovery(self):
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded-analysis"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture required capability timeout"),
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                interrupted,
                self._valid_tool_lines(
                    "bounded registry evidence",
                    tool_name="web_search",
                ),
                self._stop_lines(
                    "status: WARN/degraded\nEvidence: evidence.md with provenance"
                ),
            ],
            max_iterations=4,
            delegated=True,
            required_capability_tools=["web_search"],
            enabled_tool="web_search",
        )

        self.assertEqual(3, len(requests))
        # A declared required capability is deterministic on the first
        # eligible delegated turn. Recovery must preserve (not introduce)
        # that exact mandatory-call boundary.
        self.assertEqual("required", requests[0]["body"].get("tool_choice"))
        self.assertFalse(requests[0]["body"].get("parallel_tool_calls", True))
        self.assertEqual(2048, requests[0]["body"].get("max_tokens"))
        self.assertEqual(
            {"enable_thinking": False},
            requests[0]["body"].get("chat_template_kwargs"),
        )
        self.assertEqual("required", requests[1]["body"].get("tool_choice"))
        self.assertFalse(requests[1]["body"].get("parallel_tool_calls", True))
        self.assertEqual(2048, requests[1]["body"].get("max_tokens"))
        self.assertEqual(
            {"enable_thinking": False},
            requests[1]["body"].get("chat_template_kwargs"),
        )
        self.assertNotIn("tool_choice", requests[2]["body"])
        dispatch_mock.assert_awaited_once()
        gate = next(
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_reasoning_only_stream_recovery"
        )
        self.assertTrue(gate["payload"]["required_capability_call"])
        self.assertEqual(
            ["web_search"],
            gate["payload"]["required_capability_candidates"],
        )
        requests_after_dispatch = [
            event for event in events
            if event.get("event_type") == "debug.llm.request"
            and event.get("payload", {}).get("iteration", 0) > 2
        ]
        self.assertTrue(requests_after_dispatch)
        self.assertFalse(
            requests_after_dispatch[-1]["payload"].get(
                "delegate_required_capability_at_request"
            )
        )
        self.assertEqual("done", events[-1]["type"])

    async def test_reasoning_interrupt_with_whitespace_content_is_not_zero_content(self):
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": " ",
                        "reasoning_content": "discarded-analysis",
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture stream timeout"),
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [interrupted, self._stop_lines("must not be requested")],
            max_iterations=3,
            delegated=True,
        )

        self.assertEqual(["stream"], [request["kind"] for request in requests])
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and str(event.get("payload", {}).get("gate") or "").startswith(
                "delegate_reasoning_only"
            )
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("error", events[-1]["type"])

    async def test_reasoning_only_stream_recovery_second_interrupt_is_terminal(self):
        def interrupted(secret):
            return [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {"reasoning_content": secret},
                        "finish_reason": None,
                    }],
                }),
                asyncio.TimeoutError("fixture stream timeout"),
            ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                interrupted("first-discarded-analysis"),
                interrupted("second-discarded-analysis"),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=4,
            delegated=True,
        )

        self.assertEqual(["stream", "stream"], [
            request["kind"] for request in requests
        ])
        dispatch_mock.assert_not_awaited()
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "delegate_reasoning_only_stream_recovery"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual("error", events[-1]["type"])

    async def test_primary_reasoning_only_stream_interrupt_recovers_as_new_turn(self):
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "primary-analysis"},
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture stream timeout"),
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [interrupted, self._stop_lines("bounded primary answer")],
            max_iterations=3,
            delegated=False,
            enabled_tool="",
            task_text="Explain the bounded result.",
            requested_max_tokens=262_144,
            provider_context_length=303_872,
        )

        self.assertEqual(["stream", "stream"], [
            request["kind"] for request in requests
        ])
        dispatch_mock.assert_not_awaited()
        self.assertNotIn(
            "primary-analysis",
            json.dumps(requests[1]["body"]["messages"], ensure_ascii=False),
        )
        recovery_history = json.dumps(
            requests[1]["body"]["messages"], ensure_ascii=False
        )
        self.assertIn("new logical turn, not an HTTP replay", recovery_history)
        self.assertNotIn("chat_template_kwargs", requests[0]["body"])
        self.assertEqual(
            {"enable_thinking": False},
            requests[1]["body"].get("chat_template_kwargs"),
        )
        self.assertEqual(262_144, requests[1]["body"].get("max_tokens"))
        recovery_debug = next(
            event for event in events
            if event.get("event_type") == "debug.llm.request"
            and event.get("payload", {}).get(
                "primary_reasoning_only_stream_recovery"
            )
        )["payload"]
        self.assertIsNone(recovery_debug["primary_phase_max_tokens"])
        self.assertEqual(262_144, recovery_debug["requested_max_tokens"])
        gate = next(
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "primary_reasoning_only_stream_recovery"
        )
        self.assertEqual(0, gate["payload"]["current_turn_dispatch_count"])
        self.assertFalse(gate["payload"]["http_request_replayed"])
        self.assertTrue(gate["payload"]["logical_recovery_turn"])
        self.assertTrue(gate["payload"]["prior_runtime_state_preserved"])
        self.assertTrue(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "primary_reasoning_only_stream_recovery"
            for event in events
        ))
        self.assertTrue(any(
            event.get("event_type")
            == "debug.primary.reasoning_only_stream_recovery.requested"
            for event in events
        ))
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))
        self.assertEqual("done", events[-1]["type"])

    async def test_primary_mandatory_frontier_has_phase_budget_and_no_thinking(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("bounded.md"),
                self._stop_lines("read completed"),
            ],
            max_iterations=3,
            delegated=False,
            enabled_tool="read_file",
            task_text="read a file",
            requested_max_tokens=262_144,
            provider_context_length=303_872,
        )

        self.assertEqual(2, len(requests))
        dispatch_mock.assert_awaited_once()
        first = requests[0]["body"]
        self.assertEqual("required", first.get("tool_choice"))
        self.assertEqual(8_192, first.get("max_tokens"))
        self.assertEqual(
            {"enable_thinking": False},
            first.get("chat_template_kwargs"),
        )
        # The cap is phase-local: terminal synthesis retains the caller's
        # larger allowance when the provider context can admit it.
        self.assertGreater(requests[1]["body"].get("max_tokens", 0), 8_192)
        first_debug = next(
            event for event in events
            if event.get("event_type") == "debug.llm.request"
            and event.get("payload", {}).get("iteration") == 1
        )["payload"]
        self.assertTrue(first_debug["primary_mandatory_control_tool_frontier"])
        self.assertTrue(first_debug["primary_bounded_argument_tool_frontier"])
        self.assertEqual(8_192, first_debug["primary_phase_max_tokens"])
        self.assertEqual(262_144, first_debug["caller_requested_max_tokens"])
        self.assertEqual(8_192, first_debug["requested_max_tokens"])
        self.assertTrue(first_debug["thinking_disabled"])

    async def test_primary_large_argument_frontier_keeps_caller_budget_and_no_thinking(self):
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines(
                    tool_name="write_file",
                    arguments={
                        "filepath": "large-report.md",
                        "content": "complete report body",
                    },
                ),
                *[
                    self._stop_lines(
                        f"artifact enforcement fixture stop {index}"
                    )
                    for index in range(12)
                ],
            ],
            # The dispatch mock intentionally does not materialize a file, so
            # artifact enforcement may consume its bounded follow-up turns.
            # Those fixture turns are irrelevant to the first wire request.
            max_iterations=3,
            delegated=False,
            enabled_tool="write_file",
            task_text="write a file named x.md",
            required_tool_surface=True,
            requested_max_tokens=262_144,
            provider_context_length=303_872,
        )

        self.assertGreaterEqual(len(requests), 1)
        dispatch_mock.assert_awaited_once()
        first = requests[0]["body"]
        self.assertEqual("required", first.get("tool_choice"))
        # write_file carries the artifact in its arguments.  The mandatory
        # control boundary still disables hidden thinking, but must not apply
        # the 8K small-argument cap and truncate the content payload.
        self.assertEqual(262_144, first.get("max_tokens"))
        self.assertEqual(
            {"enable_thinking": False},
            first.get("chat_template_kwargs"),
        )
        first_debug = next(
            event for event in events
            if event.get("event_type") == "debug.llm.request"
            and event.get("payload", {}).get("iteration") == 1
        )["payload"]
        self.assertTrue(first_debug["primary_mandatory_control_tool_frontier"])
        self.assertFalse(first_debug["primary_bounded_argument_tool_frontier"])
        self.assertIsNone(first_debug["primary_phase_max_tokens"])
        self.assertEqual(262_144, first_debug["requested_max_tokens"])
        self.assertEqual(262_144, first_debug["caller_requested_max_tokens"])
        self.assertTrue(first_debug["thinking_disabled"])

    async def test_primary_reasoning_recovery_preserves_prior_dispatch_state(self):
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "reasoning_content": "discarded-synthesis-analysis",
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture post-dispatch reasoning timeout"),
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                self._valid_tool_lines("bounded.md"),
                interrupted,
                self._stop_lines("read completed from retained result"),
            ],
            max_iterations=4,
            delegated=False,
            enabled_tool="read_file",
            task_text="read a file",
            requested_max_tokens=262_144,
            provider_context_length=303_872,
        )

        self.assertEqual(3, len(requests))
        dispatch_mock.assert_awaited_once()
        recovery_messages = requests[2]["body"]["messages"]
        self.assertTrue(any(
            message.get("role") == "tool" for message in recovery_messages
        ))
        self.assertNotIn(
            "discarded-synthesis-analysis",
            json.dumps(recovery_messages, ensure_ascii=False),
        )
        self.assertEqual(
            {"enable_thinking": False},
            requests[2]["body"].get("chat_template_kwargs"),
        )
        self.assertEqual(262_144, requests[2]["body"].get("max_tokens"))
        gate = next(
            event for event in events
            if event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "primary_reasoning_only_stream_recovery"
        )
        self.assertEqual(0, gate["payload"]["current_turn_dispatch_count"])
        self.assertEqual(1, gate["payload"]["prior_dispatch_count_preserved"])
        self.assertFalse(gate["payload"]["mandatory_control_tool_frontier"])
        self.assertTrue(gate["payload"]["prior_runtime_state_preserved"])
        self.assertFalse(any(
            event.get("event_type") == "run.failed" for event in events
        ))

    async def test_primary_reasoning_only_recovery_second_interrupt_is_terminal(self):
        def interrupted(secret):
            return [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {"reasoning_content": secret},
                        "finish_reason": None,
                    }],
                }),
                asyncio.TimeoutError("fixture stream timeout"),
            ]

        requests, dispatch_mock, events = await self._run_stream_sequence(
            [
                interrupted("first-primary-analysis"),
                interrupted("second-primary-analysis"),
                self._stop_lines("must not be requested"),
            ],
            max_iterations=4,
            delegated=False,
            enabled_tool="",
            task_text="Explain the bounded result.",
        )

        self.assertEqual(["stream", "stream"], [
            request["kind"] for request in requests
        ])
        dispatch_mock.assert_not_awaited()
        self.assertEqual(1, sum(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "primary_reasoning_only_stream_recovery"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "primary_reasoning_only_stream_recovery_exhausted",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(1, failed["payload"][
            "primary_reasoning_only_recovery_count"
        ])
        self.assertTrue(failed["payload"][
            "primary_reasoning_only_recovery_turn"
        ])
        self.assertFalse(failed["payload"]["http_request_replayed"])
        exhausted = next(
            event for event in events
            if event.get("event_type")
            == "debug.primary.reasoning_only_stream_recovery.exhausted"
        )
        self.assertEqual(1, exhausted["payload"]["recovery_count"])
        self.assertFalse(exhausted["payload"]["http_request_replayed"])
        self.assertEqual("error", events[-1]["type"])

    async def test_primary_partial_visible_content_never_uses_reasoning_recovery(self):
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "content": "visible-prefix",
                        "reasoning_content": "primary-analysis",
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture stream timeout"),
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [interrupted, self._stop_lines("must not be requested")],
            max_iterations=3,
            delegated=False,
            enabled_tool="",
            task_text="Explain the bounded result.",
        )

        self.assertEqual(["stream"], [request["kind"] for request in requests])
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "primary_reasoning_only_stream_recovery"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )

    async def test_primary_tool_fragment_never_uses_reasoning_recovery(self):
        interrupted = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "reasoning_content": "primary-analysis",
                        "tool_calls": [{
                            "index": 0,
                            "id": "partial-call",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":"partial',
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
            asyncio.TimeoutError("fixture stream timeout"),
        ]
        requests, dispatch_mock, events = await self._run_stream_sequence(
            [interrupted, self._stop_lines("must not be requested")],
            max_iterations=3,
            delegated=False,
            enabled_tool="read_file",
            task_text="read a file",
        )

        self.assertEqual(["stream"], [request["kind"] for request in requests])
        dispatch_mock.assert_not_awaited()
        self.assertFalse(any(
            event.get("event_type") == "debug.gate.continuation"
            and event.get("payload", {}).get("gate")
            == "primary_reasoning_only_stream_recovery"
            for event in events
        ))
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertGreater(failed["payload"]["stream_fragment_count"], 0)
        self.assertEqual(
            "stream_interrupted_after_partial",
            failed["payload"]["finish_reason"],
        )

    async def test_repair_request_history_excludes_corrupt_args_and_reasoning(self):
        requests, dispatch_mock, _events = await self._run_stream_sequence([
            self._partial_corrupt_lines(),
            self._valid_tool_lines(),
            self._stop_lines(),
        ])

        dispatch_mock.assert_awaited_once()
        repair_messages = json.dumps(
            requests[1]["body"]["messages"], ensure_ascii=False
        )
        self.assertIn("safe-visible-anchor", repair_messages)
        self.assertIn("single bounded repair turn", repair_messages)
        self.assertIn("only one fresh, complete tool call", repair_messages)
        self.assertIn("Do not emit multiple calls", repair_messages)
        self.assertNotIn("raw-corrupt-argument-secret", repair_messages)
        self.assertNotIn("hidden-reasoning-secret", repair_messages)
        all_events = json.dumps(_events, ensure_ascii=False)
        self.assertNotIn("raw-corrupt-argument-secret", all_events)
        self.assertNotIn("hidden-reasoning-secret", all_events)

    async def test_stream_corrupt_nonstream_valid_dispatches_once(self):
        fallback_payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": "fallback-secret-content-must-be-ignored",
                    "tool_calls": [{
                        "id": "fallback-secret-provider-id",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"filepath":"fallback-secret.md"}',
                        },
                    }],
                },
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
            },
        }

        requests, dispatch_mock, events = await self._run_case(
            fallback_payload,
        )

        self.assertEqual(2, len(requests))
        self.assertEqual(["stream", "fallback"], [item["kind"] for item in requests])
        self.assertTrue(requests[0]["body"]["stream"])
        self.assertIn("stream_options", requests[0]["body"])
        self.assertFalse(requests[1]["body"]["stream"])
        self.assertNotIn("stream_options", requests[1]["body"])
        self.assertEqual(
            requests[0]["body"]["messages"],
            requests[1]["body"]["messages"],
        )
        self.assertEqual(
            requests[0]["body"]["tools"],
            requests[1]["body"]["tools"],
        )
        dispatch_mock.assert_awaited_once()
        self.assertEqual(
            {"filepath": "fallback-secret.md"},
            dispatch_mock.await_args.args[1],
        )

        visible_text = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        self.assertEqual("", visible_text)
        self.assertNotIn("fallback-secret-content", visible_text)

        fallback_events = [
            event for event in events
            if str(event.get("event_type") or "").startswith(
                "debug.tool.stream.fallback."
            )
        ]
        self.assertEqual(
            [
                "debug.tool.stream.fallback.requested",
                "debug.tool.stream.fallback.completed",
            ],
            [event["event_type"] for event in fallback_events],
        )
        fallback_debug = json.dumps(fallback_events, ensure_ascii=False)
        self.assertNotIn("stream-secret", fallback_debug)
        self.assertNotIn("fallback-secret", fallback_debug)
        self.assertNotIn("filepath", fallback_debug)

    async def test_turn_boundary_reports_post_arbitration_disposition_without_debug(self):
        fallback_payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [{
                        "id": "fallback-call",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"filepath":"recovered.md"}',
                        },
                    }],
                },
            }],
        }
        boundaries = []

        async def capture_boundary(boundary):
            boundaries.append(dict(boundary))

        _requests, dispatch_mock, events = await self._run_case(
            fallback_payload,
            stream_finish_reason="stop",
            turn_boundary_sink=capture_boundary,
            debug_trace=False,
        )

        dispatch_mock.assert_awaited_once()
        self.assertEqual(["started", "finished"], [
            boundary["phase"] for boundary in boundaries
        ])
        self.assertEqual("tool_calls", boundaries[-1]["finish_reason"])
        self.assertFalse(any(
            str(event.get("event_type") or "").startswith("debug.")
            for event in events
        ))

    async def test_stream_corrupt_after_content_without_budget_is_terminal(self):
        fallback_payload = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "must-not-run",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"filepath":"must-not-run.md"}',
                        },
                    }],
                },
            }],
        }

        requests, dispatch_mock, events = await self._run_case(
            fallback_payload,
            content="already-visible-content",
        )

        self.assertEqual(["stream"], [item["kind"] for item in requests])
        dispatch_mock.assert_not_awaited()
        visible_text = "".join(
            str(event.get("content") or "")
            for event in events
            if event.get("type") == "delta"
        )
        # Explicit file actions buffer pre-tool prose until the required call
        # occurs, so a corrupt mixed prose/tool sample cannot expose an
        # unverified answer before the run fails closed.
        self.assertEqual("", visible_text)
        self.assertEqual("error", events[-1]["type"])
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_corrupt_after_content",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(
            "iteration_budget_exhausted",
            failed["payload"]["repair_unavailable_reason"],
        )
        unavailable = next(
            event for event in events
            if event.get("event_type")
            == "debug.tool.stream.continuation_repair.unavailable"
        )
        self.assertEqual(
            "iteration_budget_exhausted",
            unavailable["payload"]["reason"],
        )
        self.assertFalse(any(
            str(event.get("event_type") or "").startswith(
                "debug.tool.stream.fallback."
            )
            for event in events
        ))

    async def test_fallback_malformed_is_terminal_without_dispatch_or_retry(self):
        fallback_payload = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "fallback-secret-provider-id",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"filepath":"fallback-secret',
                        },
                    }],
                },
            }],
        }

        requests, dispatch_mock, events = await self._run_case(fallback_payload)

        # One corrupt stream plus exactly one non-stream fallback.  With the
        # default tiny iteration budget in this fixture, no exact-one replan
        # or evidence synthesis has budget remaining, so the bounded recovery
        # ladder is exhausted immediately after the single fallback attempt.
        self.assertEqual(2, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertEqual("error", events[-1]["type"])
        self.assertIn("bounded recovery ladder", events[-1]["msg"])
        failed = next(
            event for event in events if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "provider_tool_stream_recovery_exhausted",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(
            "iteration_budget_exhausted",
            failed["payload"]["replan_unavailable_reason"],
        )
        self.assertEqual(
            "iteration_budget_exhausted",
            failed["payload"]["synthesis_unavailable_reason"],
        )
        fallback_events = [
            event for event in events
            if str(event.get("event_type") or "").startswith(
                "debug.tool.stream.fallback."
            )
        ]
        self.assertEqual(1, sum(
            event["event_type"].endswith(".requested")
            for event in fallback_events
        ))
        self.assertEqual(1, sum(
            event["event_type"].endswith(".failed")
            for event in fallback_events
        ))
        self.assertFalse(any(
            event["event_type"].endswith(".completed")
            for event in fallback_events
        ))
        failed_debug = json.dumps(fallback_events, ensure_ascii=False)
        self.assertNotIn("stream-secret", failed_debug)
        self.assertNotIn("fallback-secret", failed_debug)
        self.assertNotIn("filepath", failed_debug)

    async def test_fallback_transport_failure_does_not_enter_retry_loop(self):
        requests, dispatch_mock, events = await self._run_case(
            RuntimeError("transport-secret-must-not-leak")
        )

        self.assertEqual(2, len(requests))
        dispatch_mock.assert_not_awaited()
        self.assertEqual("error", events[-1]["type"])
        fallback_failed = next(
            event for event in events
            if event.get("event_type") == "debug.tool.stream.fallback.failed"
        )
        encoded = json.dumps(fallback_failed, ensure_ascii=False)
        self.assertIn("transport_error", encoded)
        self.assertIn("RuntimeError", encoded)
        self.assertNotIn("transport-secret", encoded)

    async def test_fallback_http_failure_does_not_enter_retry_loop(self):
        requests, dispatch_mock, events = await self._run_case(
            _FakeJSONResponse({"provider_error": "http-secret"}, status_code=503)
        )

        self.assertEqual(2, len(requests))
        dispatch_mock.assert_not_awaited()
        fallback_failed = next(
            event for event in events
            if event.get("event_type") == "debug.tool.stream.fallback.failed"
        )
        encoded = json.dumps(fallback_failed, ensure_ascii=False)
        self.assertIn("http_error", encoded)
        self.assertIn("503", encoded)
        self.assertNotIn("http-secret", encoded)


if __name__ == "__main__":
    unittest.main()
