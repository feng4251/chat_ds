import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import _semantic_skill_selector_arguments, run_stream


class SemanticSkillActivationTests(unittest.IsolatedAsyncioTestCase):
    provider = {
        "id": "mock-semantic-selector",
        "base_url": "http://model.invalid/v1",
        "api_model": "mock-semantic-selector",
        "api_key": "EMPTY",
        "protocol": "openai",
        "provider": "mock",
        "context_length": 64_000,
        "is_multimodal": False,
    }
    available_tools = [
        "skill_view",
        "skills_list",
        "run_skill_python",
        "run_skill_script",
        "run_declared_command",
        "skill_http_get",
        "delegate_task",
        "write_file",
        "execute_code",
    ]

    async def _run(
        self,
        request: str,
        records: list[dict],
        selector,
        *,
        extra_selector_tool_calls: list[dict] | None = None,
    ):
        selector_requests: list[dict] = []
        stream_requests: list[dict] = []
        dispatches: list[tuple[str, dict]] = []

        class FakePostResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeStreamResponse:
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

            async def post(self, url, **kwargs):
                body = kwargs.get("json") or {}
                selector_requests.append(body)
                arguments = selector(body)
                return FakePostResponse({
                    "choices": [{
                        "message": {
                            "tool_calls": [{
                                "id": "selector-call",
                                "type": "function",
                                "function": {
                                    "name": "select_session_skill",
                                    "arguments": json.dumps(arguments),
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 3,
                        "total_tokens": 14,
                    },
                })

            def stream(self, method, url, **kwargs):
                body = kwargs.get("json") or {}
                stream_requests.append(body)
                selector_exposed = any(
                    (tool.get("function") or {}).get("name")
                    == "select_session_skill"
                    for tool in body.get("tools") or []
                    if isinstance(tool, dict)
                )
                if selector_exposed:
                    arguments = selector(body)
                    if arguments.get("decision") != "none":
                        tool_calls = [{
                            "index": 0,
                            "id": "semantic-selector-call",
                            "function": {
                                "name": "select_session_skill",
                                "arguments": json.dumps(arguments),
                            },
                        }]
                        tool_calls.extend(extra_selector_tool_calls or [])
                        return FakeStreamResponse([
                            "data: " + json.dumps({
                                "choices": [{
                                    "delta": {"tool_calls": tool_calls},
                                    "finish_reason": None,
                                }],
                            }),
                            "data: " + json.dumps({
                                "choices": [{
                                    "delta": {},
                                    "finish_reason": "tool_calls",
                                }],
                            }),
                            "data: [DONE]",
                        ])
                return FakeStreamResponse([
                    "data: " + json.dumps({
                        "choices": [{
                            "delta": {"content": "bounded response"},
                            "finish_reason": None,
                        }],
                    }),
                    "data: " + json.dumps({
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                    }),
                    "data: [DONE]",
                ])

        by_name = {str(record["name"]): record for record in records}

        def loaded_package(path, **kwargs):
            path_text = str(path)
            record = next(
                (
                    item for name, item in by_name.items()
                    if name in path_text
                ),
                {},
            )
            return {
                "name": record.get("name"),
                "description": record.get("description", ""),
                "content": record.get(
                    "content",
                    "# Instructions\n\nAnswer using the selected writing guidance.",
                ),
                "linked_files": {},
                "workflow_contract": None,
                "package_diagnostics": {
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                },
            }

        async def fake_dispatch(name, args, *, context):
            dispatches.append((name, dict(args)))
            record = by_name.get(str(args.get("name") or ""), {})
            return json.dumps({
                "name": record.get("name"),
                "description": record.get("description", ""),
                "content": record.get(
                    "content",
                    "# Instructions\n\nAnswer using the selected writing guidance.",
                ),
                "linked_files": {},
                "workflow_contract": None,
                "package_diagnostics": {
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                },
            })

        schemas = [{
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        } for name in self.available_tools]

        def schemas_for(names):
            allowed = set(names or [])
            return [
                schema for schema in schemas
                if schema["function"]["name"] in allowed
            ]

        with tempfile.TemporaryDirectory() as temp_dir:
            normalized_records = [
                {
                    **record,
                    "path": str(Path(temp_dir) / str(record["name"]) / "SKILL.md"),
                    "skill_dir": str(Path(temp_dir) / str(record["name"])),
                    "scope": str(record.get("scope") or "session"),
                }
                for record in records
            ]
            by_name = {
                str(record["name"]): record for record in normalized_records
            }
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", side_effect=schemas_for),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("agent_loop.settings.agent_debug_trace", True),
                patch("agent_loop.settings.complex_report_max_iterations", 1),
                patch("skills.loader.load_skill_content", side_effect=loaded_package),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=normalized_records,
                ),
                patch(
                    "skills.scanner.skill_runnable_script_resources",
                    return_value=(),
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-semantic-selector",
                        [{"role": "user", "content": request}],
                        self.available_tools,
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-semantic-skill",
                        session_id="s-semantic-skill",
                        max_iterations=1,
                    )
                ]
        return selector_requests, stream_requests, dispatches, events

    def test_nonstream_selector_rejects_ambiguous_provider_shapes(self):
        arguments = json.dumps({
            "decision": "candidate",
            "candidate_names": ["one"],
            "reason": "one exact match",
        })
        call = {
            "function": {
                "name": "select_session_skill",
                "arguments": arguments,
            },
        }
        valid_choice = {
            "message": {"tool_calls": [call]},
            "finish_reason": "tool_calls",
        }
        for payload, expected in (
            (
                {"choices": [valid_choice, valid_choice]},
                "response_choices_invalid",
            ),
            (
                {"choices": [{**valid_choice, "finish_reason": "length"}]},
                "typed_finish_reason_invalid",
            ),
            (
                {
                    "choices": [valid_choice],
                    "content": [{"type": "tool_use"}],
                },
                "response_protocol_shape_ambiguous",
            ),
        ):
            with self.subTest(expected=expected):
                decision, error = _semantic_skill_selector_arguments(
                    payload,
                    {"one"},
                )
                self.assertIsNone(decision)
                self.assertEqual(expected, error)

    def test_nonstream_selector_accepts_stop_with_one_schema_valid_forced_call(self):
        payload = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "select_session_skill",
                            "arguments": json.dumps({
                                "decision": "candidate",
                                "candidate_names": ["one"],
                                "reason": "one exact metadata match",
                            }),
                        },
                    }],
                },
                "finish_reason": "stop",
            }],
        }

        decision, error = _semantic_skill_selector_arguments(payload, {"one"})

        self.assertEqual("", error)
        self.assertEqual(("one",), decision["candidate_names"])

    def test_nonstream_selector_stop_does_not_weaken_call_validation(self):
        valid_arguments = json.dumps({
            "decision": "candidate",
            "candidate_names": ["one"],
            "reason": "one exact metadata match",
        })
        call = {
            "function": {
                "name": "select_session_skill",
                "arguments": valid_arguments,
            },
        }
        cases = (
            ({"message": {"tool_calls": []}, "finish_reason": "stop"},
             "typed_call_missing_or_multiple"),
            ({"message": {"tool_calls": [call, call]}, "finish_reason": "stop"},
             "typed_call_missing_or_multiple"),
            ({
                "message": {"tool_calls": [{
                    "function": {
                        "name": "select_session_skill",
                        "arguments": "{not-json",
                    },
                }]},
                "finish_reason": "stop",
            }, "typed_arguments_malformed_json"),
            ({
                "message": {"tool_calls": [{
                    "function": {
                        "name": "select_session_skill",
                        "arguments": json.dumps({
                            "decision": "candidate",
                            "candidate_names": ["two"],
                            "reason": "attempt outside the disclosed page",
                        }),
                    },
                }]},
                "finish_reason": "stop",
            }, "typed_candidate_outside_page"),
        )
        for choice, expected in cases:
            with self.subTest(expected=expected):
                decision, error = _semantic_skill_selector_arguments(
                    {"choices": [choice]},
                    {"one"},
                )
                self.assertIsNone(decision)
                self.assertEqual(expected, error)

    @staticmethod
    def _catalog_from_request(body: dict) -> list[dict]:
        for message in body.get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str):
                continue
            if "chatds.session-skill-catalog.v1" in content:
                return json.loads(content.rsplit("\n", 1)[-1])["catalog"]
        message = body["messages"][-1]["content"]
        return json.loads(message)["catalog"]

    async def test_standard_skill_synonym_uses_typed_metadata_selection(self):
        records = [{
            "name": "prose-refiner",
            "description": (
                "Improve tone, clarity, fluency, and readability in written passages."
            ),
            "content": (
                "# Instructions\n\n"
                "- Use execute_code to validate the transformed text metrics.\n"
                "- Return the polished prose.\n"
            ),
        }]

        def selector(body):
            catalog = self._catalog_from_request(body)
            self.assertEqual({"name", "description"}, set(catalog[0]))
            return {
                "decision": "candidate",
                "candidate_names": ["prose-refiner"],
                "reason": "The request asks to smooth written prose.",
            }

        selector_requests, stream_requests, dispatches, events = await self._run(
            "Could you smooth out this paragraph so it sounds natural?",
            records,
            selector,
        )

        self.assertEqual(0, len(selector_requests))
        self.assertEqual(
            [("skill_view", {"name": "prose-refiner", "file_path": ""})],
            dispatches,
        )
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        relevance = started["payload"]["session_skill_relevance"]
        self.assertEqual([], relevance["selected_skills"])
        self.assertEqual("semantic_model_pending", relevance["reason"])
        self.assertEqual("pending", relevance["semantic_status"])
        exposed_on_selection_turn = {
            tool["function"]["name"]
            for tool in stream_requests[0].get("tools") or []
        }
        self.assertEqual(
            {"select_session_skill"},
            exposed_on_selection_turn,
        )
        self.assertTrue(any(
            event.get("event_type") == "debug.session_skill.semantic_selection"
            and event.get("payload", {}).get("status") == "selected"
            and event.get("payload", {}).get("inspection_authority_only") is True
            and event.get("payload", {}).get("ordinary_tool_receipt_created") is False
            for event in events
        ))
        self.assertFalse(any(
            event.get("tool_name") == "select_session_skill"
            and str(event.get("event_type") or "").startswith("tool.")
            for event in events
        ))

    async def test_third_language_request_can_select_english_metadata(self):
        records = [
            {
                "name": "prose-refiner",
                "description": "Improve clarity and readability of written passages.",
            },
            {
                "name": "table-auditor",
                "description": "Check numeric tables for duplicate rows and totals.",
            },
        ]

        def selector(_body):
            return {
                "decision": "candidate",
                "candidate_names": ["prose-refiner"],
                "reason": "The Japanese request asks to improve prose readability.",
            }

        _, _, dispatches, events = await self._run(
            "この文章を自然で読みやすい表現に直してください。",
            records,
            selector,
        )

        self.assertEqual("prose-refiner", dispatches[0][1]["name"])
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        self.assertEqual(
            "semantic",
            started["payload"]["session_skill_relevance"]["selection_method"],
        )

    async def test_description_signal_after_384_chars_is_preserved(self):
        records = [
            {
                "name": "long-metadata-skill",
                "description": (
                    "Background metadata. " * 24
                    + "Improve clarity and readability of written passages."
                ),
            },
            {
                "name": "table-auditor",
                "description": "Check numeric tables for duplicate rows and totals.",
            },
        ]

        def selector(body):
            catalog = self._catalog_from_request(body)
            long_record = next(
                item for item in catalog
                if item["name"] == "long-metadata-skill"
            )
            self.assertGreater(len(long_record["description"]), 384)
            self.assertIn("Improve clarity", long_record["description"])
            return {
                "decision": "candidate",
                "candidate_names": ["long-metadata-skill"],
                "reason": "The relevant signal is present near the metadata tail.",
            }

        _, _, dispatches, _ = await self._run(
            "この文章を自然で読みやすい表現に直してください。",
            records,
            selector,
        )

        self.assertEqual("long-metadata-skill", dispatches[0][1]["name"])

    async def test_130_entry_catalog_is_scanned_in_bounded_pages(self):
        records = [
            {
                "name": f"catalog-item-{index:03d}",
                "description": f"Handle unrelated catalog operation {index}.",
            }
            for index in range(129)
        ] + [{
            "name": "zzzz-prose-refiner",
            "description": "Improve tone and readability of written passages.",
        }]

        def selector(body):
            names = [item["name"] for item in self._catalog_from_request(body)]
            if "zzzz-prose-refiner" in names:
                return {
                    "decision": "candidate",
                    "candidate_names": ["zzzz-prose-refiner"],
                    "reason": "This page contains the one writing match.",
                }
            return {
                "decision": "none",
                "candidate_names": [],
                "reason": "No entry on this page applies.",
            }

        selector_requests, stream_requests, dispatches, events = await self._run(
            "Could you smooth out this paragraph so it sounds natural?",
            records,
            selector,
        )

        self.assertEqual(2, len(selector_requests))
        self.assertEqual(1, len(stream_requests))
        self.assertLessEqual(
            max(len(self._catalog_from_request(body)) for body in selector_requests),
            128,
        )
        self.assertEqual("zzzz-prose-refiner", dispatches[0][1]["name"])
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        relevance = started["payload"]["session_skill_relevance"]
        self.assertEqual(2, relevance["semantic_pages"])
        self.assertEqual("semantic_selected", relevance["reason"])

    async def test_semantic_none_is_explicit_and_grants_no_skill_bridge(self):
        records = [{
            "name": "table-auditor",
            "description": "Check numeric tables for duplicate rows and totals.",
        }]

        def selector(_body):
            return {
                "decision": "none",
                "candidate_names": [],
                "reason": "The catalog has no travel-planning Skill.",
            }

        _, stream_requests, dispatches, events = await self._run(
            "Help me arrange a relaxing vacation itinerary.",
            records,
            selector,
        )

        self.assertEqual([], dispatches)
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        relevance = started["payload"]["session_skill_relevance"]
        self.assertEqual("semantic_model_pending", relevance["reason"])
        self.assertEqual("pending", relevance["semantic_status"])
        self.assertTrue(any(
            event.get("event_type") == "debug.session_skill.semantic_selection"
            and event.get("payload", {}).get("status") == "none"
            and event.get("payload", {}).get("selection_method")
            == "primary_model_no_call"
            for event in events
        ))
        exposed = {
            tool["function"]["name"]
            for tool in stream_requests[0].get("tools") or []
        }
        self.assertNotIn("skill_view", exposed)

    async def test_semantic_ambiguity_is_explicit_and_grants_no_skill_bridge(self):
        records = [
            {
                "name": "prose-refiner-a",
                "description": "Improve tone and readability of prose.",
            },
            {
                "name": "prose-refiner-b",
                "description": "Polish fluency and clarity of written passages.",
            },
        ]

        def selector(_body):
            return {
                "decision": "ambiguous",
                "candidate_names": ["prose-refiner-a", "prose-refiner-b"],
                "reason": "Both entries are comparably applicable.",
            }

        selector_requests, stream_requests, dispatches, events = await self._run(
            "Create a detailed multi-stage report that polishes this prose.",
            records,
            selector,
        )

        self.assertEqual(2, len(selector_requests))
        self.assertEqual(1, len(stream_requests))
        self.assertEqual([], dispatches)
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        relevance = started["payload"]["session_skill_relevance"]
        self.assertEqual("semantic_ambiguous", relevance["reason"])
        self.assertEqual("ambiguous", relevance["semantic_status"])
        self.assertTrue(any(
            event.get("event_type") == "debug.session_skill.semantic_selection"
            and event.get("payload", {}).get("status") == "ambiguous"
            for event in events
        ))
        exposed = {
            tool["function"]["name"]
            for tool in stream_requests[0].get("tools") or []
        }
        self.assertNotIn("skill_view", exposed)

    async def test_complex_deliverable_resolves_one_page_before_workflow_tools(self):
        records = [
            {
                "name": "trial-simulator",
                "description": (
                    "Generate clinical trial cohorts and Phase I/II/III study data."
                ),
            },
            {
                "name": "table-auditor",
                "description": "Check numeric tables for duplicate rows and totals.",
            },
        ]

        def selector(body):
            exposed = {
                (tool.get("function") or {}).get("name")
                for tool in body.get("tools") or []
            }
            self.assertEqual({"select_session_skill"}, exposed)
            return {
                "decision": "candidate",
                "candidate_names": ["trial-simulator"],
                "reason": "The request is a multi-phase clinical trial plan.",
            }

        selector_requests, stream_requests, dispatches, events = await self._run(
            "请制定一个完整的一、二、三期新药临床开发计划。",
            records,
            selector,
        )

        self.assertEqual(1, len(selector_requests))
        self.assertEqual(
            [("skill_view", {"name": "trial-simulator", "file_path": ""})],
            dispatches,
        )
        self.assertFalse(any(
            (tool.get("function") or {}).get("name")
            in {"write_file", "execute_code", "delegate_task"}
            for request in selector_requests
            for tool in request.get("tools") or []
        ))
        self.assertTrue(any(
            event.get("event_type") == "debug.session_skill.semantic_selection"
            and event.get("payload", {}).get("status") == "selected"
            for event in events
        ))

    async def test_nonclinical_cross_language_complex_skill_uses_same_router(self):
        records = [
            {
                "name": "financial-model-builder",
                "description": (
                    "Build integrated three-statement financial models, DCF "
                    "valuations, and acquisition sensitivity analyses."
                ),
            },
            {
                "name": "prose-refiner",
                "description": "Improve tone and readability of prose.",
            },
        ]

        def selector(body):
            self.assertEqual(
                {"select_session_skill"},
                {
                    (tool.get("function") or {}).get("name")
                    for tool in body.get("tools") or []
                },
            )
            return {
                "decision": "candidate",
                "candidate_names": ["financial-model-builder"],
                "reason": (
                    "The Chinese request asks for an acquisition model and "
                    "valuation deliverable."
                ),
            }

        selector_requests, _, dispatches, events = await self._run(
            "请为拟收购公司建立完整三表财务模型、DCF估值和敏感性分析报告。",
            records,
            selector,
        )

        self.assertEqual(1, len(selector_requests))
        self.assertEqual(
            [(
                "skill_view",
                {"name": "financial-model-builder", "file_path": ""},
            )],
            dispatches,
        )
        self.assertTrue(any(
            event.get("event_type") == "debug.session_skill.semantic_selection"
            and event.get("payload", {}).get("status") == "selected"
            for event in events
        ))

    async def test_explicit_exact_name_bypasses_semantic_model(self):
        records = [{
            "name": "prose-refiner",
            "description": "Improve tone and readability of written passages.",
        }]

        def selector(_body):
            self.fail("explicit exact Skill activation must not call semantic selector")

        selector_requests, stream_requests, dispatches, events = await self._run(
            "Use prose-refiner to improve this paragraph.",
            records,
            selector,
        )

        self.assertEqual([], selector_requests)
        self.assertEqual([], dispatches)
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        self.assertEqual(
            "explicit_skill_request",
            started["payload"]["skill_workflow_activation"],
        )
        exposed = {
            tool["function"]["name"]
            for tool in stream_requests[0].get("tools") or []
        }
        self.assertIn("skill_view", exposed)

    async def test_selector_cannot_choose_name_outside_disclosed_page(self):
        records = [{
            "name": "table-auditor",
            "description": "Check numeric tables for duplicate rows and totals.",
        }]

        def selector(_body):
            return {
                "decision": "candidate",
                "candidate_names": ["invented-skill"],
                "reason": "Attempt to escape the typed enum.",
            }

        _, stream_requests, dispatches, events = await self._run(
            "Please smooth out this paragraph so it sounds natural.",
            records,
            selector,
        )

        self.assertEqual([], dispatches)
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        relevance = started["payload"]["session_skill_relevance"]
        self.assertEqual("pending", relevance["semantic_status"])
        self.assertTrue(any(
            event.get("event_type") == "debug.session_skill.semantic_selection"
            and event.get("payload", {}).get("status") == "unavailable"
            and event.get("payload", {}).get("failure_kind")
            == "typed_candidate_outside_page"
            for event in events
        ))
        exposed = {
            tool["function"]["name"]
            for tool in stream_requests[0].get("tools") or []
        }
        self.assertNotIn("skill_view", exposed)

    async def test_selector_mixed_with_ordinary_tool_is_discarded_atomically(self):
        records = [{
            "name": "table-auditor",
            "description": "Check numeric tables for duplicate rows and totals.",
        }]

        def selector(_body):
            return {
                "decision": "candidate",
                "candidate_names": ["table-auditor"],
                "reason": "A syntactically valid candidate decision.",
            }

        _, stream_requests, dispatches, events = await self._run(
            "Please smooth out this paragraph so it sounds natural.",
            records,
            selector,
            extra_selector_tool_calls=[{
                "index": 1,
                "id": "mixed-write-call",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({
                        "filepath": "must-not-exist.md",
                        "content": "must not dispatch",
                    }),
                },
            }],
        )

        self.assertEqual(1, len(stream_requests))
        self.assertEqual([], dispatches)
        self.assertTrue(any(
            event.get("event_type") == "debug.session_skill.semantic_selection"
            and event.get("payload", {}).get("status") == "unavailable"
            and event.get("payload", {}).get("failure_kind")
            == "selector_stream_batch_invalid"
            for event in events
        ))
        self.assertFalse(any(
            str(event.get("event_type") or "").startswith("tool.")
            and event.get("tool_name") in {
                "select_session_skill", "write_file",
            }
            for event in events
        ))

    async def test_trivial_arithmetic_does_not_add_selector_model_call(self):
        records = [{
            "name": "table-auditor",
            "description": "Check numeric tables for duplicate rows and totals.",
        }]

        def selector(_body):
            self.fail("trivial arithmetic must not spend a selector request")

        selector_requests, stream_requests, dispatches, events = await self._run(
            "2 + 2 = ?",
            records,
            selector,
        )

        self.assertEqual([], selector_requests)
        self.assertEqual(1, len(stream_requests))
        self.assertEqual([], dispatches)
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        self.assertEqual(
            "skipped",
            started["payload"]["session_skill_relevance"]["semantic_status"],
        )

    async def test_unrelated_fact_or_rewrite_uses_one_primary_model_request(self):
        records = [{
            "name": "table-auditor",
            "description": "Check numeric tables for duplicate rows and totals.",
        }]

        def selector(_body):
            return {
                "decision": "none",
                "candidate_names": [],
                "reason": "No catalog entry applies.",
            }

        requests = (
            "What is the boiling point of water at sea level?",
            "Rewrite this sentence in a warmer tone: The meeting is moved.",
        )
        for request in requests:
            with self.subTest(request=request):
                selector_requests, stream_requests, dispatches, events = (
                    await self._run(request, records, selector)
                )
                self.assertEqual([], selector_requests)
                self.assertEqual(1, len(stream_requests))
                self.assertEqual([], dispatches)
                exposed = {
                    tool["function"]["name"]
                    for tool in stream_requests[0].get("tools") or []
                }
                self.assertEqual({"select_session_skill"}, exposed)
                self.assertTrue(any(
                    event.get("event_type")
                    == "debug.session_skill.semantic_selection"
                    and event.get("payload", {}).get("status") == "none"
                    for event in events
                ))


if __name__ == "__main__":
    unittest.main()
