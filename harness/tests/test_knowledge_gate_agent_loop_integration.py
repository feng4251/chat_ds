import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import run_stream
from knowledge_gate_runtime import canonical_json_sha256
from tools.mcp_contract import (
    build_mcp_tool_descriptor,
    freeze_mcp_catalog,
    preflight_mcp_tool_call,
)


def _tool_call_response(
    tool_name: str,
    arguments: dict,
    *,
    call_id: str,
) -> list[str]:
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


def _empty_authority(candidates: list[dict]) -> dict:
    return {
        "resource_grants": [],
        "script_grants": [],
        "process_only_script_grants": [],
        "script_authority_grants": [],
        "package_grants": [],
        "command_grants": [],
        "http_get_grants": [],
        "http_post_grants": [],
        "tool_names": [
            candidate["tool_name"]
            for candidate in candidates
            if candidate.get("tool_name")
        ],
        "receipt_bindings": candidates,
    }


class KnowledgeGateAgentLoopIntegrationTests(
    unittest.IsolatedAsyncioTestCase
):
    provider = {
        "id": "mock-knowledge-gate",
        "base_url": "http://model.invalid/v1",
        "api_model": "mock-knowledge-gate",
        "api_key": "EMPTY",
        "protocol": "openai",
        "provider": "mock",
        "context_length": 64_000,
        "is_multimodal": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
    }

    async def _run(
        self,
        responses: list[list[str]],
        *,
        plan: dict | None,
        authority: dict | None,
        allow_session_mcp: bool,
        frozen_catalog=None,
        dispatch_mcp=None,
        enabled_tools: list[str] | None = None,
        source: str = "delegate",
        agent_kind: str = "delegate",
    ):
        request_bodies: list[dict] = []

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
                request_bodies.append(kwargs["json"])
                return FakeResponse(responses.pop(0))

        digest = canonical_json_sha256(plan) if plan is not None else ""
        patches = [
            patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
            patch("agent_loop.build_system_prompt", return_value="system"),
            patch("agent_loop.load_workspace_context", return_value=""),
            patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
            patch("skills.scanner.find_all_skills", return_value=[]),
        ]
        if frozen_catalog is not None:
            patches.extend([
                patch(
                    "tools.mcp_client.connect_all_for_user",
                    AsyncMock(return_value={"evidence": True}),
                ),
                patch(
                    "tools.mcp_client.freeze_session_mcp_catalog",
                    return_value=frozen_catalog,
                ),
            ])
        if dispatch_mcp is not None:
            patches.extend([
                patch(
                    "tools.mcp_client.preflight_frozen_session_mcp_tool_call",
                    side_effect=lambda name, args, *_a, **_k: (
                        preflight_mcp_tool_call(
                            frozen_catalog.get(name),
                            args,
                        )
                    ),
                ),
                patch(
                    "tools.mcp_client.dispatch_mcp_tool",
                    dispatch_mcp,
                ),
            ])

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "workspace_context.WORKSPACE_ROOT",
                Path(temp_dir),
            ):
                entered = []
                try:
                    for active_patch in patches:
                        entered.append(active_patch)
                        active_patch.start()
                    gate_kwargs = (
                        {
                            "knowledge_gate_plan": plan,
                            "knowledge_gate_plan_sha256": digest,
                            "knowledge_gate_candidate_authority": authority,
                        }
                        if plan is not None
                        else {}
                    )
                    events = [
                        event async for event in run_stream(
                            "mock-knowledge-gate",
                            [{"role": "user", "content": "Run the bounded task."}],
                            enabled_tools or [],
                            provider_override=self.provider,
                            allow_session_mcp=allow_session_mcp,
                            user_id="u-knowledge-gate",
                            session_id="s-knowledge-gate",
                            source=source,
                            agent_kind=agent_kind,
                            max_iterations=4,
                            **gate_kwargs,
                        )
                    ]
                finally:
                    for active_patch in reversed(entered):
                        active_patch.stop()
        return request_bodies, events

    async def test_provider_receives_plan_bound_exact_decision_schema(self):
        checks = [
            {
                "id": check_id,
                "question": f"Question {check_id}?",
                "branches": [
                    {"outcome": outcome, "action": "", "group_ids": []}
                    for outcome in ("yes", "no", "unknown")
                ],
                "legacy_ambiguous": False,
            }
            for check_id in ("evidence-ready", "conflict-resolved")
        ]
        plan = {
            "schema_version": 1,
            "worker_id": "worker-generic",
            "owner_skill": "generic-skill",
            "checks": checks,
            "groups": [],
            "candidates": [],
        }
        digest = canonical_json_sha256(plan)
        responses = [
            _tool_call_response(
                "submit_knowledge_gate_decisions",
                {
                    "plan_sha256": digest,
                    "decisions": [
                        {
                            "check_id": "evidence-ready",
                            "outcome": "yes",
                            "reason": "Evidence is present.",
                        },
                        {
                            "check_id": "conflict-resolved",
                            "outcome": "unknown",
                            "reason": "The conflict remains explicit.",
                        },
                    ],
                },
                call_id="call-decision",
            ),
            _stop_response(
                "status: WARN\nEvidence is bounded; one conflict is unresolved."
            ),
        ]

        bodies, _events = await self._run(
            responses,
            plan=plan,
            authority=_empty_authority([]),
            allow_session_mcp=False,
            enabled_tools=["submit_knowledge_gate_decisions"],
        )

        decision = bodies[0]
        self.assertEqual("required", decision["tool_choice"])
        self.assertIs(False, decision["parallel_tool_calls"])
        self.assertEqual(1, len(decision["tools"]))
        function = decision["tools"][0]["function"]
        self.assertEqual(
            "submit_knowledge_gate_decisions",
            function["name"],
        )
        decisions = function["parameters"]["properties"]["decisions"]
        self.assertEqual(2, decisions["minItems"])
        self.assertEqual(2, decisions["maxItems"])
        item_properties = decisions["items"]["properties"]
        self.assertEqual(
            ["evidence-ready", "conflict-resolved"],
            item_properties["check_id"]["enum"],
        )
        self.assertEqual(
            ["yes", "no", "unknown"],
            item_properties["outcome"]["enum"],
        )
        self.assertNotIn(
            "submit_knowledge_gate_decisions",
            [
                tool["function"]["name"]
                for tool in bodies[1].get("tools", [])
            ],
        )

    async def test_direct_chat_strips_registry_default_decision_control(self):
        bodies, events = await self._run(
            [_stop_response("A direct answer.")],
            plan=None,
            authority=None,
            allow_session_mcp=False,
            enabled_tools=["submit_knowledge_gate_decisions"],
            source="chat",
            agent_kind="primary",
        )

        self.assertEqual(1, len(bodies))
        self.assertNotIn(
            "submit_knowledge_gate_decisions",
            [
                tool["function"]["name"]
                for tool in bodies[0].get("tools", [])
            ],
        )
        resolved = next(
            event
            for event in events
            if event.get("event_type") == "tool_surface.resolved"
        )
        self.assertNotIn(
            "submit_knowledge_gate_decisions",
            resolved["payload"]["effective_tools"],
        )

    async def test_conditional_mcp_is_hidden_then_activated(self):
        descriptor = build_mcp_tool_descriptor(
            server_name="evidence",
            tool_name="lookup",
            public_name="mcp_evidence_lookup",
            description="Lookup bounded evidence.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        catalog = freeze_mcp_catalog([descriptor])
        bound = catalog.get("mcp_evidence_lookup")
        candidate = {
            "candidate_id": "candidate-mcp-evidence",
            "kind": "mcp_tool",
            "tool_name": "mcp_evidence_lookup",
            "tool_names": ["mcp_evidence_lookup"],
            "schema_sha256": bound.schema_sha256,
            "descriptor_sha256": bound.descriptor_sha256,
        }
        plan = self._mcp_plan(candidate)
        digest = canonical_json_sha256(plan)
        responses = [
            _tool_call_response(
                "submit_knowledge_gate_decisions",
                {
                    "plan_sha256": digest,
                    "decisions": [{
                        "check_id": "remote-evidence",
                        "outcome": "yes",
                        "reason": "Remote evidence is required.",
                    }],
                },
                call_id="call-decision",
            ),
            _tool_call_response(
                "mcp_evidence_lookup",
                {"query": "bounded"},
                call_id="call-mcp",
            ),
            _stop_response("status: PASS\nEvidence lookup completed."),
        ]
        dispatch_mcp = AsyncMock(return_value=json.dumps({
            "status": "success",
            "evidence": ["bounded"],
        }))

        bodies, events = await self._run(
            responses,
            plan=plan,
            authority=_empty_authority([candidate]),
            allow_session_mcp=True,
            frozen_catalog=catalog,
            dispatch_mcp=dispatch_mcp,
        )

        self.assertEqual(
            ["submit_knowledge_gate_decisions"],
            [tool["function"]["name"] for tool in bodies[0]["tools"]],
        )
        self.assertEqual(
            ["mcp_evidence_lookup"],
            [tool["function"]["name"] for tool in bodies[1]["tools"]],
        )
        dispatch_mcp.assert_awaited_once()
        self.assertFalse(any(
            event.get("event_type") == "run.failed"
            for event in events
        ))

    async def test_conditional_mcp_descriptor_drift_fails_before_dispatch(self):
        original = freeze_mcp_catalog([build_mcp_tool_descriptor(
            server_name="evidence",
            tool_name="lookup",
            public_name="mcp_evidence_lookup",
            description="Lookup bounded evidence.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )])
        original_descriptor = original.get("mcp_evidence_lookup")
        candidate = {
            "candidate_id": "candidate-mcp-evidence",
            "kind": "mcp_tool",
            "tool_name": "mcp_evidence_lookup",
            "tool_names": ["mcp_evidence_lookup"],
            "schema_sha256": original_descriptor.schema_sha256,
            "descriptor_sha256": original_descriptor.descriptor_sha256,
        }
        plan = self._mcp_plan(candidate)
        digest = canonical_json_sha256(plan)
        drifted = freeze_mcp_catalog([build_mcp_tool_descriptor(
            server_name="evidence",
            tool_name="lookup",
            public_name="mcp_evidence_lookup",
            description="Lookup bounded evidence.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "expanded": {"type": "boolean"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )])
        responses = [_tool_call_response(
            "submit_knowledge_gate_decisions",
            {
                "plan_sha256": digest,
                "decisions": [{
                    "check_id": "remote-evidence",
                    "outcome": "yes",
                    "reason": "Remote evidence is required.",
                }],
            },
            call_id="call-decision",
        )]
        dispatch_mcp = AsyncMock(return_value="must-not-dispatch")

        bodies, events = await self._run(
            responses,
            plan=plan,
            authority=_empty_authority([candidate]),
            allow_session_mcp=True,
            frozen_catalog=drifted,
            dispatch_mcp=dispatch_mcp,
        )

        self.assertEqual(1, len(bodies))
        self.assertEqual(
            ["submit_knowledge_gate_decisions"],
            [tool["function"]["name"] for tool in bodies[0]["tools"]],
        )
        dispatch_mcp.assert_not_awaited()
        failed = next(
            event
            for event in events
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            "knowledge_gate_activation_failed",
            failed["payload"]["finish_reason"],
        )
        self.assertEqual(
            "knowledge_gate_activation_toctou_failed",
            failed["payload"]["terminal_reason"],
        )

    @staticmethod
    def _mcp_plan(candidate: dict) -> dict:
        return {
            "schema_version": 1,
            "worker_id": "worker-mcp",
            "owner_skill": "generic-skill",
            "checks": [{
                "id": "remote-evidence",
                "question": "Is remote evidence required?",
                "branches": [
                    {
                        "outcome": "yes",
                        "action": "",
                        "group_ids": ["group-mcp"],
                    },
                    {"outcome": "no", "action": "", "group_ids": []},
                    {"outcome": "unknown", "action": "", "group_ids": []},
                ],
                "legacy_ambiguous": False,
            }],
            "groups": [{
                "id": "group-mcp",
                "check_id": "remote-evidence",
                "outcome": "yes",
                "mode": "one_of",
                "candidate_ids": [candidate["candidate_id"]],
                "selectors": ["mcp_evidence_lookup"],
                "unresolved_selectors": [],
            }],
            "candidates": [candidate],
        }
