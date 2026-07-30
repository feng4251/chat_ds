import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from agent_loop import (
    HarnessRunState,
    _compiled_intent_classifier_input,
    _compiled_skill_inspection_target,
    _parse_intent_selections,
    run_stream,
)
from prompt.builder import DEFAULT_AGENT_IDENTITY, build_system_prompt
from tools.context import ToolContext
from tools.delegation import _run_child


SKILL = "generic-natural-language-router"


def _plan():
    return {
        "selection": "pending_intent",
        "intent_classification": {
            "dimensions": [{
                "id": "request_kind",
                "required": True,
                # No redundant values list: legal values are map keys.
                "description": "Classify the requested depth.",
                "mappings": {
                    "worker_map": {
                        "brief": ["worker-summary"],
                        "deep": ["worker-research"],
                    },
                    "resource_map": {
                        "brief": "references/brief.md",
                        "deep": "references/deep.md",
                    },
                },
            }],
        },
        "workers": {
            "worker-summary": {},
            "worker-research": {},
        },
        "declared_routes": [],
        "available_bootstrap_sources": [],
        "available_aggregation_steps": [],
    }


def _context(*tools):
    return ToolContext(
        user_id="u-intent",
        session_id="s-intent",
        model_id="model",
        provider_config={
            "base_url": "http://model.invalid/v1",
            "api_model": "model",
            "context_length": 64_000,
        },
        enabled_tools=tools,
        enabled_user_skills=("unrelated-persistent-skill",),
        run_id="parent",
        root_run_id="root",
    )


class BoundedIntentClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_empty_tools_are_rejected_for_non_intent_steps(self):
        with patch("agent_loop.run_stream") as run_model:
            for task in (
                {
                    "goal": "run worker",
                    "worker_id": "worker-a",
                    "step_type": "worker",
                    "tools": [],
                },
                {
                    "goal": "aggregate",
                    "step_type": "aggregation",
                    "tools": [],
                },
            ):
                with self.subTest(step_type=task["step_type"]):
                    result = await _run_child(task, _context("read_file"), 0)
                    self.assertEqual(result["status"], "error")
                    self.assertIn("reserved for the bounded intent", result["error"])
        run_model.assert_not_called()

    async def test_natural_language_classifier_has_no_tools_then_parent_closes_exact_mapping(self):
        plan = _plan()
        classifier_input = _compiled_intent_classifier_input(
            plan,
            "Please perform a deep investigation of this subject.",
        )
        self.assertEqual(
            classifier_input["dimensions"][0]["values"],
            ["brief", "deep"],
        )
        observed = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed.update({
                "messages": messages,
                "tools": tools,
                "kwargs": kwargs,
            })
            yield {
                "type": "delta",
                "content": (
                    "request_kind — PASS: deep — evidence: deep investigation\n"
                    'INTENT_SELECTIONS_JSON: {"request_kind":"deep"}'
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch("tools.delegation.registry_dispatch", new_callable=AsyncMock) as dispatch,
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/intent.md",
            ),
        ):
            child = await _run_child(
                {
                    "goal": "classify the compiled request",
                    "skill_name": SKILL,
                    "step_type": "intent_classification",
                    "step_id": "intent-classification",
                    "required_output_ids": ["request_kind"],
                    "context_text": json.dumps(classifier_input),
                    "tools": [],
                    "max_iterations": 30,
                },
                _context("skill_view", "read_file"),
                0,
            )

        self.assertEqual(child["status"], "completed")
        self.assertEqual(child["intent_selections"], {"request_kind": "deep"})
        self.assertEqual(
            64,
            len(str(child.get("authority_snapshot_sha256") or "")),
        )
        self.assertEqual(
            [],
            child["authority_snapshot"]["script_grants"],
        )
        self.assertEqual(
            [],
            child["authority_snapshot"]["package_grants"],
        )
        self.assertEqual(observed["tools"], [])
        self.assertEqual(observed["kwargs"]["max_iterations"], 2)
        self.assertEqual(observed["kwargs"]["max_tokens"], 4096)
        self.assertEqual(observed["kwargs"]["enabled_user_skills"], [])
        self.assertFalse(observed["kwargs"]["allow_session_mcp"])
        self.assertFalse(observed["kwargs"]["include_session_context"])
        self.assertNotIn("skill_view", observed["messages"][0]["content"])
        dispatch.assert_not_awaited()

        state = HarnessRunState()
        state.session_skill_names.add(SKILL)
        state.viewed_skill_names.add(SKILL)
        state.viewed_skill_files[SKILL] = {"__manifest__"}
        state.skill_available_categories[SKILL] = {"references"}
        state.skill_category_files[SKILL] = {
            "references": ["references/brief.md", "references/deep.md"],
        }
        state.skill_workflow_contracts[SKILL] = {
            "orchestrator_files": ["orchestration/main.yaml"],
        }
        state.skill_execution_plans[SKILL] = plan
        update = state.record_delegate_task(
            {
                "goal": "classify",
                "skill_name": SKILL,
                "step_type": "intent_classification",
                "step_id": "intent-classification",
            },
            {"results": [child]},
        )
        self.assertEqual(update["pending_step_ids"], ["intent-classification"])
        pending = state.skill_pending_intent_resource_closure[SKILL]
        self.assertEqual(pending["required_resources"], ["references/deep.md"])
        self.assertEqual(pending["missing_resources"], ["references/deep.md"])
        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        target, error = _compiled_skill_inspection_target(state, reason)
        self.assertEqual(error, "")
        self.assertEqual(
            target,
            {"name": SKILL, "file_path": "references/deep.md"},
        )
        state.record_skill_view(
            target,
            {
                "success": True,
                "name": SKILL,
                "file": "references/deep.md",
                "content": "deep contract",
            },
        )
        self.assertIn(SKILL, state.skill_completed_intent)

    async def test_missing_footer_is_bounded_retryable_failure(self):
        observed = {}

        async def reasoning_only(model_id, messages, tools, **kwargs):
            observed.update(kwargs)
            yield {"type": "reasoning_delta", "content": "thinking" * 100}
            yield {
                "type": "error",
                "msg": "Model hit max output tokens.",
            }

        with patch("agent_loop.run_stream", reasoning_only):
            result = await _run_child(
                {
                    "goal": "classify",
                    "step_type": "intent_classification",
                    "required_output_ids": ["request_kind"],
                    "context_text": json.dumps(
                        _compiled_intent_classifier_input(_plan(), "deep review")
                    ),
                    "tools": [],
                    "max_iterations": 30,
                },
                _context("skill_view"),
                0,
            )

        self.assertEqual(observed["max_iterations"], 2)
        self.assertEqual(observed["max_tokens"], 4096)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["retryable"])
        self.assertIn("INTENT_SELECTIONS_JSON", result["error"])
        self.assertEqual(result["tool_audit"]["attempted_tools"], [])

    async def test_optional_warn_null_does_not_degrade_intent_child(self):
        classifier_input = {
            "schema": "chatds.intent-classifier.v1",
            "original_user_request": "Perform a deep review.",
            "dimensions": [
                {
                    "id": "request_kind",
                    "required": True,
                    "values": ["brief", "deep"],
                },
                {
                    "id": "design_type",
                    "required": False,
                    "nullable": True,
                    "values": ["parallel", "adaptive"],
                },
            ],
        }

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield {
                "type": "delta",
                "content": (
                    "request_kind — PASS: deep — evidence: explicit request\n"
                    "design_type — WARN: null — evidence: not specified\n"
                    "INTENT_SELECTIONS_JSON: "
                    '{"request_kind":"deep","design_type":null}'
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/intent.md",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "classify every compiled dimension",
                    "skill_name": SKILL,
                    "step_type": "intent_classification",
                    "step_id": "intent-classification",
                    "required_output_ids": [
                        "request_kind",
                        "design_type",
                    ],
                    "context_text": json.dumps(classifier_input),
                    "tools": [],
                },
                _context(),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("complete", result["completion_quality"])
        self.assertEqual({
            "request_kind": "deep",
            "design_type": None,
        }, result["intent_selections"])
        self.assertEqual(
            "none",
            result["completion_quality_audit"]["declaration_source"],
        )

    async def test_required_or_non_null_warn_intent_fails_closed(self):
        classifier_input = {
            "schema": "chatds.intent-classifier.v1",
            "original_user_request": "Perform a deep parallel review.",
            "dimensions": [
                {
                    "id": "request_kind",
                    "required": True,
                    "values": ["brief", "deep"],
                },
                {
                    "id": "design_type",
                    "required": False,
                    "nullable": True,
                    "values": ["parallel", "adaptive"],
                },
            ],
        }
        cases = {
            "required_warn": (
                "request_kind — WARN: deep — evidence: ambiguous depth\n"
                "design_type — PASS: parallel — evidence: explicit request\n"
                "INTENT_SELECTIONS_JSON: "
                '{"request_kind":"deep","design_type":"parallel"}'
            ),
            "optional_non_null_warn": (
                "request_kind — PASS: deep — evidence: explicit request\n"
                "design_type — WARN: parallel — evidence: ambiguous design\n"
                "INTENT_SELECTIONS_JSON: "
                '{"request_kind":"deep","design_type":"parallel"}'
            ),
        }

        for label, model_content in cases.items():
            async def fake_run_stream(
                model_id,
                messages,
                tools,
                *,
                _model_content=model_content,
                **kwargs,
            ):
                yield {"type": "delta", "content": _model_content}
                yield {"type": "done", "finish_reason": "stop"}

            with self.subTest(label=label):
                with (
                    patch("agent_loop.run_stream", fake_run_stream),
                    patch(
                        "tools.delegation.persist_result_for_history",
                    ) as persist,
                ):
                    result = await _run_child(
                        {
                            "goal": "classify every compiled dimension",
                            "skill_name": SKILL,
                            "step_type": "intent_classification",
                            "step_id": "intent-classification",
                            "required_output_ids": [
                                "request_kind",
                                "design_type",
                            ],
                            "context_text": json.dumps(classifier_input),
                            "tools": [],
                        },
                        _context(),
                        0,
                    )

            self.assertEqual("error", result["status"])
            self.assertTrue(result["retryable"])
            self.assertIn(
                "may use WARN:null",
                result["error"],
            )
            persist.assert_not_called()

    async def test_optional_missing_policies_and_defaults_are_exact(self):
        cases = (
            {
                "label": "optional_plain_omitted",
                "dimension": {
                    "id": "style",
                    "required": False,
                    "values": ["compact", "detailed"],
                },
                "line": "style — WARN: null — evidence: omitted",
                "footer": '{"request_kind":"deep"}',
                "completed": True,
            },
            {
                "label": "nullable_omitted",
                "dimension": {
                    "id": "style",
                    "required": False,
                    "nullable": True,
                    "values": ["compact", "detailed"],
                },
                "line": "style — WARN: null — evidence: omitted",
                "footer": '{"request_kind":"deep"}',
                "completed": True,
            },
            {
                "label": "skip_policy_omitted",
                "dimension": {
                    "id": "style",
                    "required": False,
                    "on_missing": "skip",
                    "values": ["compact", "detailed"],
                },
                "line": "style — WARN: null — evidence: skipped",
                "footer": '{"request_kind":"deep"}',
                "completed": True,
            },
            {
                "label": "default_warn_rejected",
                "dimension": {
                    "id": "style",
                    "required": False,
                    "nullable": True,
                    "default": "compact",
                    "values": ["compact", "detailed"],
                },
                "line": "style — WARN: null — evidence: omitted",
                "footer": '{"request_kind":"deep","style":null}',
                "completed": False,
            },
            {
                "label": "default_pass_accepted",
                "dimension": {
                    "id": "style",
                    "required": False,
                    "nullable": True,
                    "default": "compact",
                    "values": ["compact", "detailed"],
                },
                "line": "style — PASS: compact — evidence: declared default",
                "footer": '{"request_kind":"deep","style":null}',
                "completed": True,
            },
        )
        for case in cases:
            classifier_input = {
                "schema": "chatds.intent-classifier.v1",
                "original_user_request": "Perform a deep review.",
                "dimensions": [
                    {
                        "id": "request_kind",
                        "required": True,
                        "values": ["brief", "deep"],
                    },
                    case["dimension"],
                ],
            }
            model_content = (
                "request_kind — PASS: deep — evidence: explicit request\n"
                + str(case["line"])
                + "\nINTENT_SELECTIONS_JSON: "
                + str(case["footer"])
            )

            async def fake_run_stream(
                model_id,
                messages,
                tools,
                *,
                _model_content=model_content,
                **kwargs,
            ):
                yield {"type": "delta", "content": _model_content}
                yield {"type": "done", "finish_reason": "stop"}

            with self.subTest(label=case["label"]):
                with (
                    patch("agent_loop.run_stream", fake_run_stream),
                    patch(
                        "tools.delegation.persist_result_for_history",
                        return_value="results/intent.md",
                    ),
                ):
                    result = await _run_child(
                        {
                            "goal": "classify every compiled dimension",
                            "skill_name": SKILL,
                            "step_type": "intent_classification",
                            "step_id": "intent-classification",
                            "required_output_ids": [
                                "request_kind",
                                "style",
                            ],
                            "context_text": json.dumps(classifier_input),
                            "tools": [],
                        },
                        _context(),
                        0,
                    )

            self.assertEqual(
                "completed" if case["completed"] else "error",
                result["status"],
            )


class IntentClassifierSessionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_stream_context_switch_skips_workspace_memory_goal_and_skills(self):
        response_lines = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "classified"},
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            }),
            "data: [DONE]",
        ]
        request_bodies = []

        class FakeResponse:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aiter_lines(self):
                for line in response_lines:
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
                request_bodies.append(kwargs["json"])
                return FakeResponse()

        provider = {
            "id": "classifier",
            "base_url": "http://model.invalid/v1",
            "api_model": "classifier",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": False,
        }
        workspace = Mock(return_value="WORKSPACE_SECRET")
        goal = AsyncMock(return_value={"objective": "GOAL_SECRET"})
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.get_schemas", Mock(return_value=[])),
                patch("agent_loop.load_workspace_context", workspace),
                patch("agent_loop._fetch_goal", goal),
                patch(
                    "prompt.builder._build_memory_block",
                    Mock(return_value="MEMORY_SECRET"),
                ) as memory,
            ):
                events = [
                    event
                    async for event in run_stream(
                        "classifier",
                        [{"role": "user", "content": "CLASSIFIER_INPUT"}],
                        [],
                        user_id="u",
                        session_id="s",
                        provider_override=provider,
                        enabled_user_skills=["SKILL_SECRET"],
                        enforce_session_skill_workflow=False,
                        allow_session_mcp=False,
                        include_session_context=False,
                        max_iterations=1,
                    )
                ]

        workspace.assert_not_called()
        goal.assert_not_awaited()
        memory.assert_not_called()
        self.assertTrue(any(event.get("type") == "done" for event in events))
        sent = json.dumps(request_bodies[0], ensure_ascii=False)
        self.assertIn("CLASSIFIER_INPUT", sent)
        self.assertIn(DEFAULT_AGENT_IDENTITY[:40], sent)
        for secret in (
            "WORKSPACE_SECRET", "MEMORY_SECRET", "GOAL_SECRET", "SKILL_SECRET",
        ):
            self.assertNotIn(secret, sent)


class IntentClassifierProjectionTests(unittest.TestCase):
    def test_failed_unparsed_selection_never_reports_completed_resource_closure(self):
        state = HarnessRunState()
        state.skill_execution_plans[SKILL] = _plan()
        update = state.record_delegate_task(
            {
                "goal": "classify",
                "skill_name": SKILL,
                "step_type": "intent_classification",
                "step_id": "intent-classification",
            },
            {"results": [{
                "status": "error",
                "skill_name": SKILL,
                "step_type": "intent_classification",
                "step_id": "intent-classification",
                "result_path": "results/intent.md",
                "result_chars": 0,
                "intent_selections": None,
                "tool_audit": {},
                "error": "missing footer",
                "failure_class": "agent_contract_noncompliance",
                "retryable": True,
            }]},
        )
        self.assertEqual(update["retryable_failed_step_ids"], ["intent-classification"])
        closure = state.skill_intent_results[SKILL]["resource_closure"]
        self.assertEqual(closure["status"], "failed")
        self.assertFalse(closure["selection_resolved"])
        self.assertEqual(closure["required_resources"], [])
        self.assertNotIn(SKILL, state.skill_completed_intent)

    def test_mapping_keys_are_parent_validated_as_declared_values(self):
        parsed, error = _parse_intent_selections(
            _plan(),
            {"intent_selections": {"request_kind": "deep"}},
        )
        self.assertEqual(error, None)
        self.assertEqual(parsed, {"request_kind": "deep"})
        _, error = _parse_intent_selections(
            _plan(),
            {"intent_selections": {"request_kind": "invented"}},
        )
        self.assertIn("undeclared", error)

    def test_minimal_system_prompt_omits_all_session_tiers(self):
        with patch(
            "prompt.builder._build_memory_block",
            Mock(return_value="MEMORY_SECRET"),
        ) as memory:
            prompt = build_system_prompt(
                user_id="u",
                session_id="s",
                system_message="CALLER_SECRET",
                workspace_context="WORKSPACE_SECRET",
                goal={"objective": "GOAL_SECRET"},
                enabled_user_skills=["SKILL_SECRET"],
                enabled_tools=[],
                include_session_context=False,
            )
        memory.assert_not_called()
        self.assertEqual(prompt, DEFAULT_AGENT_IDENTITY)


if __name__ == "__main__":
    unittest.main()
