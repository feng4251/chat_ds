import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import (
    _knowledge_gate_pending_resource_coordinate_matches,
    run_stream,
)
from knowledge_gate_runtime import canonical_json_sha256
from skill_capability_plan import canonical_https_prefix
from tools.context import ToolContext
from tools.delegation import _exact_knowledge_gate_candidate_grants
from tools.isolated_skill_executor import compute_skill_package_digest
from tools.registry import dispatch as native_registry_dispatch
from tools.skill_http import _retrieval_receipt
from tools.mcp_contract import (
    build_mcp_tool_descriptor,
    freeze_mcp_catalog,
    preflight_mcp_tool_call,
)
from tests.support.scripted_provider import (
    ScriptedProvider,
    ScriptedTurn,
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
        "sandbox_egress_grants": [],
        "tool_names": list(dict.fromkeys(
            candidate["tool_name"]
            for candidate in candidates
            if candidate.get("tool_name")
        )),
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

    def test_resource_coordinate_match_distinguishes_paging_from_drift(self):
        candidate = {
            "candidate_id": "candidate-resource",
            "kind": "skill_resource",
            "skill_name": "evidence-adapter",
            "resource_path": "references/query.md",
        }
        plan = {
            "groups": [{
                "id": "group-resource",
                "candidate_ids": ["candidate-resource"],
            }],
            "candidates": [candidate],
        }

        self.assertTrue(
            _knowledge_gate_pending_resource_coordinate_matches(
                plan,
                ["group-resource"],
                tool_name="skill_view",
                args={
                    "name": "evidence-adapter",
                    "file_path": "references/query.md",
                    "offset": 4096,
                },
            )
        )
        self.assertFalse(
            _knowledge_gate_pending_resource_coordinate_matches(
                plan,
                ["group-resource"],
                tool_name="skill_view",
                args={
                    "name": "inactive-adapter",
                    "file_path": "references/query.md",
                },
            )
        )
        self.assertFalse(
            _knowledge_gate_pending_resource_coordinate_matches(
                plan,
                ["group-resource"],
                tool_name="skill_view",
                args={
                    "name": "evidence-adapter",
                    "file_path": "references/other.md",
                },
            )
        )

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
        extra_run_kwargs: dict | None = None,
        native_dispatch=None,
        max_iterations: int = 4,
        resolved_skill_path: Path | None = None,
    ):
        provider = ScriptedProvider(
            ScriptedTurn(tuple(lines))
            for lines in responses
        )

        digest = canonical_json_sha256(plan) if plan is not None else ""
        patches = [
            patch(
                "agent_loop.httpx.AsyncClient",
                provider.client_factory,
            ),
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
        if native_dispatch is not None:
            patches.append(patch("agent_loop.dispatch", native_dispatch))
        if resolved_skill_path is not None:
            patches.append(patch(
                "skills.scanner.resolve_skill_path",
                return_value=resolved_skill_path,
            ))

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
                            max_iterations=max_iterations,
                            **gate_kwargs,
                            **(extra_run_kwargs or {}),
                        )
                    ]
                finally:
                    for active_patch in reversed(entered):
                        active_patch.stop()
        provider.assert_exhausted()
        return [
            request["body"] for request in provider.requests
        ], events

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

    async def test_pending_gate_group_precedes_http_family_continuation(self):
        skill_temp = tempfile.TemporaryDirectory()
        self.addCleanup(skill_temp.cleanup)
        skill_root = Path(skill_temp.name) / "generic-skill"
        skill_root.mkdir()
        skill_main = skill_root / "SKILL.md"
        skill_main.write_text("# Generic Skill\n", encoding="utf-8")
        skill_main_sha256 = hashlib.sha256(
            skill_main.read_bytes()
        ).hexdigest()
        package_sha256 = compute_skill_package_digest(skill_root)
        first_prefix = "https://archive.example.test:443/records/"
        second_prefix = "https://registry.example.test:443/entries/"
        first_url = first_prefix + "?limit=25"
        second_url = second_prefix + "?limit=5"
        candidates = [
            {
                "candidate_id": "candidate-archive",
                "kind": "skill_http_prefix",
                "tool_name": "skill_http_get",
                "tool_names": ["skill_http_get"],
                "skill_name": "generic-skill",
                "skill_md_sha256": skill_main_sha256,
                "package_sha256": package_sha256,
                "url_prefix": first_prefix,
                "http_method": "GET",
            },
            {
                "candidate_id": "candidate-registry",
                "kind": "skill_http_prefix",
                "tool_name": "skill_http_get",
                "tool_names": ["skill_http_get"],
                "skill_name": "generic-skill",
                "skill_md_sha256": skill_main_sha256,
                "package_sha256": package_sha256,
                "url_prefix": second_prefix,
                "http_method": "GET",
            },
        ]
        plan = {
            "schema_version": 1,
            "worker_id": "worker-generic",
            "owner_skill": "generic-skill",
            "checks": [{
                "id": "source-check",
                "question": "Are both declared sources required?",
                "branches": [{
                    "outcome": "yes",
                    "action": "Acquire both exact sources.",
                    "group_ids": ["group-archive", "group-registry"],
                }],
                "legacy_ambiguous": False,
            }],
            "groups": [
                {
                    "id": "group-archive",
                    "check_id": "source-check",
                    "outcome": "yes",
                    "mode": "one_of",
                    "candidate_ids": ["candidate-archive"],
                    "selectors": ["skill:generic-skill/archive"],
                    "unresolved_selectors": [],
                },
                {
                    "id": "group-registry",
                    "check_id": "source-check",
                    "outcome": "yes",
                    "mode": "one_of",
                    "candidate_ids": ["candidate-registry"],
                    "selectors": ["skill:generic-skill/registry"],
                    "unresolved_selectors": [],
                },
            ],
            "candidates": candidates,
        }
        digest = canonical_json_sha256(plan)
        authority = _empty_authority(candidates)
        authority["http_get_grants"] = [
            ("generic-skill", first_prefix),
            ("generic-skill", second_prefix),
        ]
        authority["package_grants"] = [
            ("generic-skill", package_sha256),
        ]
        request_number = 0
        dispatched_urls: list[str] = []

        async def fake_dispatch(name, args, *, context):
            nonlocal request_number
            if name != "skill_http_get":
                return await native_registry_dispatch(
                    name,
                    args,
                    context=context,
                )
            request_number += 1
            url = args["url"]
            dispatched_urls.append(url)
            first_source = url.startswith(first_prefix)
            truncated = first_source and request_number == 1
            body = (
                '{"results":[{"id":"record-1"}]}'
                if first_source else
                '{"entries":[{"id":"entry-1"}]}'
            )
            prefix = first_prefix if first_source else second_prefix
            receipt = _retrieval_receipt(
                method="GET",
                request_url=url,
                request_body=None,
                response_body=body,
                pagination_scan_body=body,
                body_truncated=truncated,
                wire_body_complete=True,
                response_bytes_read=len(body.encode("utf-8")),
                response_chars_read=len(body),
                max_chars=int(args["max_chars"]),
                timeout=20,
                request_number=request_number,
                request_elapsed_ms=1,
                grants=(
                    ("generic-skill", first_prefix),
                    ("generic-skill", second_prefix),
                ),
            )
            return json.dumps({
                "status": "success",
                "request_sent": True,
                "request_number": request_number,
                "root_request_number": request_number,
                "matched_skill": "generic-skill",
                "matched_prefix_sha256": hashlib.sha256(
                    canonical_https_prefix(prefix).encode("utf-8")
                ).hexdigest(),
                "http_status": 200,
                "body": body[:10] if truncated else body,
                "body_chars": 10 if truncated else len(body),
                "body_truncated": truncated,
                "retrieval": receipt,
            })

        responses = [
            _tool_call_response(
                "submit_knowledge_gate_decisions",
                {
                    "plan_sha256": digest,
                    "decisions": [{
                        "check_id": "source-check",
                        "outcome": "yes",
                        "reason": "Both exact declared sources are required.",
                    }],
                },
                call_id="call-decision",
            ),
            _tool_call_response(
                "skill_http_get",
                {"url": first_url, "max_chars": 10, "timeout": 20},
                call_id="call-archive",
            ),
            _tool_call_response(
                "skill_http_get",
                {"url": second_url, "max_chars": 100_000, "timeout": 20},
                call_id="call-registry",
            ),
            _tool_call_response(
                "skill_http_get",
                {"url": first_url, "max_chars": 31, "timeout": 20},
                call_id="call-archive-continuation",
            ),
            _stop_response("status: PASS\nBoth exact sources are accounted for."),
        ]

        bodies, events = await self._run(
            responses,
            plan=plan,
            authority=authority,
            allow_session_mcp=False,
            enabled_tools=[
                "skill_http_get",
                "submit_knowledge_gate_decisions",
            ],
            extra_run_kwargs={
                "allowed_skill_http_prefixes": (
                    ("generic-skill", first_prefix),
                    ("generic-skill", second_prefix),
                ),
            },
            native_dispatch=fake_dispatch,
            max_iterations=6,
            resolved_skill_path=skill_main,
        )

        self.assertFalse(any(
            event.get("event_type") == "run.failed"
            for event in events
        ), events)
        self.assertEqual(
            [first_url, second_url, first_url],
            dispatched_urls,
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

    async def test_exact_preloaded_resource_closes_activated_frontier(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "generic-skill"
            (root / "references").mkdir(parents=True)
            main = root / "SKILL.md"
            resource = root / "references" / "gate.md"
            main.write_text("# Generic Skill\n", encoding="utf-8")
            resource.write_text(
                "bounded exact evidence\n",
                encoding="utf-8",
            )
            main_sha256 = hashlib.sha256(main.read_bytes()).hexdigest()
            resource_sha256 = hashlib.sha256(
                resource.read_bytes()
            ).hexdigest()
            package_sha256 = compute_skill_package_digest(root)
            candidate = {
                "candidate_id": "candidate-resource",
                "kind": "skill_resource",
                "skill_name": "generic-skill",
                "resource_path": "references/gate.md",
                "sha256": resource_sha256,
                "skill_md_sha256": main_sha256,
                "package_sha256": package_sha256,
                "tool_names": [],
            }
            plan = {
                "schema_version": 1,
                "worker_id": "worker-resource",
                "owner_skill": "generic-skill",
                "checks": [{
                    "id": "resource-required",
                    "question": "Is the exact local resource required?",
                    "branches": [{
                        "outcome": "yes",
                        "action": "Use the exact local evidence.",
                        "group_ids": ["group-resource"],
                    }],
                    "legacy_ambiguous": False,
                }],
                "groups": [{
                    "id": "group-resource",
                    "check_id": "resource-required",
                    "outcome": "yes",
                    "mode": "one_of",
                    "candidate_ids": ["candidate-resource"],
                    "selectors": [
                        "skill:generic-skill/references/gate.md",
                    ],
                    "unresolved_selectors": [],
                }],
                "candidates": [candidate],
            }
            digest = canonical_json_sha256(plan)
            context = ToolContext(
                user_id="u-knowledge-gate",
                session_id="s-knowledge-gate",
                enabled_tools=("skill_view",),
                enabled_user_skills=("generic-skill",),
                skill_execution_resource_boundary=True,
                allowed_skill_resources=(
                    ("generic-skill", "SKILL.md"),
                    ("generic-skill", "references/gate.md"),
                ),
                allowed_skill_package_digests=(
                    ("generic-skill", package_sha256),
                ),
            )
            run_id = "run-preloaded-resource"
            aggregate_sha256 = "e" * 64
            aggregate_receipt = {
                "version": 1,
                "source_count": 1,
                "kind_counts": {"skill_view": 1},
                "aggregate_sha256": aggregate_sha256,
                "complete": True,
                "run_id": run_id,
                "user_id": "u-knowledge-gate",
                "session_id": "s-knowledge-gate",
                "workspace_scope": "shared_session",
            }
            resource_receipt = {
                "version": 1,
                "run_id": run_id,
                "aggregate_sha256": aggregate_sha256,
                "skill_name": "generic-skill",
                "resource_path": "references/gate.md",
                "sha256": resource_sha256,
                "complete": True,
            }
            responses = [
                _tool_call_response(
                    "submit_knowledge_gate_decisions",
                    {
                        "plan_sha256": digest,
                        "decisions": [{
                            "check_id": "resource-required",
                            "outcome": "yes",
                            "reason": (
                                "The exact preloaded local evidence is "
                                "required."
                            ),
                        }],
                    },
                    call_id="call-resource-decision",
                ),
                _stop_response(
                    "status: PASS\nThe exact local evidence was retained."
                ),
            ]
            with patch(
                "skills.scanner.resolve_skill_path",
                return_value=main,
            ):
                authority, error = (
                    _exact_knowledge_gate_candidate_grants(
                        plan,
                        context=context,
                    )
                )
                self.assertIsNone(error)
                bodies, events = await self._run(
                    responses,
                    plan=plan,
                    authority=authority,
                    allow_session_mcp=False,
                    enabled_tools=[
                        "skill_view",
                        "submit_knowledge_gate_decisions",
                    ],
                    extra_run_kwargs={
                        "run_id": run_id,
                        "verified_preloaded_input_receipt": (
                            aggregate_receipt
                        ),
                        "preloaded_knowledge_gate_resource_receipts": [
                            resource_receipt,
                        ],
                    },
                )

        self.assertEqual(2, len(bodies))
        self.assertFalse(any(
            event.get("event_type") == "run.failed"
            for event in events
        ))
        self.assertTrue(any(
            event.get("event_type") == "run.completed"
            for event in events
        ))
        self.assertNotEqual("required", bodies[1].get("tool_choice"))

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
        decision_messages = json.dumps(
            bodies[0]["messages"],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(candidate["candidate_id"], decision_messages)
        self.assertNotIn(candidate["schema_sha256"], decision_messages)
        self.assertNotIn(
            candidate["descriptor_sha256"],
            decision_messages,
        )
        self.assertEqual(
            ["mcp_evidence_lookup"],
            [tool["function"]["name"] for tool in bodies[1]["tools"]],
        )
        activated_messages = json.dumps(
            bodies[1]["messages"],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertIn(candidate["candidate_id"], activated_messages)
        self.assertIn("mcp_evidence_lookup", activated_messages)
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
