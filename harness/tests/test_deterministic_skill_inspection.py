import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from agent_loop import (
    HarnessRunState,
    _bounded_skill_execution_exposure,
    _compiled_skill_inspection_auto_call_limit,
    _compiled_skill_inspection_target,
    _refresh_skill_workflow_continuation_allowance,
    _workflow_gate_call_error,
    run_stream,
)
from knowledge_gate import compile_symbolic_knowledge_gate
from tools.skill_runtime_profile import (
    compile_skill_runtime_profile_manifest,
)


_SKILL_MD_CONTENT = (
    "---\n"
    "name: deterministic-skill-fixture\n"
    "description: Portable test fixture for compiled Skill inspection.\n"
    "---\n"
    "# Deterministic Skill Fixture\n"
)
_SKILL_MD_SHA256 = hashlib.sha256(
    _SKILL_MD_CONTENT.encode("utf-8")
).hexdigest()


def _tool_call_response(call_id: str, name: str, arguments: dict) -> list[str]:
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": call_id,
                        "function": {
                            "name": name,
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


def _stop_response(content: str = "completed") -> list[str]:
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


def _precompiled_package(
    *,
    skill_dir: Path,
    linked_files: dict | None = None,
    workflow_contract: dict | None = None,
) -> dict:
    """Represent the scanner/loader half of these runtime-isolation tests.

    The tests mock ``skill_view`` to focus on deterministic post-inspection
    dispatch.  The production runtime now also compiles an exact capability
    surface before the first model call, so provide the same package contract
    at that boundary instead of relying on an impossible pathless scanner
    record.
    """
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        _SKILL_MD_CONTENT,
        encoding="utf-8",
    )
    for paths in (linked_files or {}).values():
        for relative_path in paths:
            target = skill_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"compiled fixture resource: {relative_path}\n",
                encoding="utf-8",
            )
    return {
        "skill_dir": str(skill_dir),
        "skill_md_sha256": _SKILL_MD_SHA256,
        "content": _SKILL_MD_CONTENT,
        "linked_files": linked_files or {},
        "workflow_contract": workflow_contract or {},
        "package_diagnostics": {
            "valid": True,
            "errors": [],
            "warnings": [],
        },
    }


class _FakeResponse:
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


class DeterministicSkillInspectionUnitTests(unittest.TestCase):
    def test_rejected_optional_gate_skill_receives_no_execution_authority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "root-skill"
            adapter_dir = Path(temp_dir) / "invalid-adapter"
            (adapter_dir / "scripts").mkdir(parents=True)
            adapter_skill_md = (
                "---\n"
                "name: invalid-adapter\n"
                "description: Invalid optional test adapter.\n"
                "---\n"
                "# Invalid adapter\n"
                "Run `scripts/query.py` or GET "
                "https://api.invalid.test/v1/search.\n"
            )
            (adapter_dir / "SKILL.md").write_text(
                adapter_skill_md,
                encoding="utf-8",
            )
            adapter_script = adapter_dir / "scripts/query.py"
            adapter_script.write_text(
                "print('query')\n",
                encoding="utf-8",
            )
            adapter_script_digest = hashlib.sha256(
                adapter_script.read_bytes()
            ).hexdigest()
            adapter = {
                "name": "invalid-adapter",
                "skill_dir": str(adapter_dir),
                "skill_md_sha256": hashlib.sha256(
                    (adapter_dir / "SKILL.md").read_bytes()
                ).hexdigest(),
                "content": adapter_skill_md,
                "linked_files": {"scripts": ["scripts/query.py"]},
                "runtime_profile_manifest": (
                    compile_skill_runtime_profile_manifest(
                        adapter_dir,
                        ("scripts/query.py",),
                    )
                ),
                "workflow_contract": {
                    "script_candidates": ["scripts/query.py"],
                    "resource_authority": {
                        "reasons": {
                            "scripts/query.py": [
                                "explicit_skill_reference",
                            ],
                        },
                    },
                    "execution_contract": {
                        "schema_version": 1,
                        "environment_contract": {
                            "commands": [{
                                "name": "git",
                                "source_files": ["SKILL.md"],
                            }],
                            "allowed_tools": [
                                "Bash(git status:*)",
                            ],
                        },
                    },
                },
                # The package scanner is authoritative.  The deliberately
                # executable-looking declarations above must remain inert once
                # this package fails that audit.
                "package_diagnostics": {
                    "valid": False,
                    "errors": ["synthetic invalid package"],
                    "warnings": [],
                },
            }
            gate = compile_symbolic_knowledge_gate(
                {
                    "checks": [{
                        "id": "coverage",
                        "question": "Is another evidence lookup required?",
                        "if_yes": {
                            "tool_groups": [{
                                "id": "evidence-source",
                                "any_of": [
                                    "skill:invalid-adapter",
                                    "web_search",
                                ],
                            }],
                        },
                    }],
                },
                skill_dir=root_dir,
                source_file="workers/research.yaml",
                worker_id="research",
            ).ir
            root = _precompiled_package(
                skill_dir=root_dir,
                linked_files={"workers": ["workers/research.yaml"]},
                workflow_contract={
                    "worker_files": ["workers/research.yaml"],
                    "requires_worker_outputs": True,
                },
            )
            plan = {
                "selection": "matched",
                "route_id": "research",
                "workers": {
                    "research": {
                        "id": "research",
                        "file": "workers/research.yaml",
                        "tools": ["web_search"],
                        "knowledge_gate_ir": gate,
                        "knowledge_gate_skill_refs": ["invalid-adapter"],
                    },
                },
                "required_workers": ["research"],
                "bootstrap_sources": [],
                "aggregation_steps": [],
            }

            exposure = _bounded_skill_execution_exposure(
                "Produce a ZEBRA report",
                [
                    "skill_view",
                    "delegate_task",
                    "web_search",
                    "submit_knowledge_gate_decisions",
                    "run_skill_process",
                    "run_skill_script",
                    "run_skill_python",
                    "skill_http_get",
                    "skill_http_post_json",
                    "run_declared_command",
                ],
                {"root-skill", "invalid-adapter"},
                {
                    "root-skill": root,
                    "invalid-adapter": adapter,
                },
                {
                    "root-skill": (),
                    "invalid-adapter": ((
                        "scripts/query.py",
                        adapter_script_digest,
                    ),),
                },
                selected_skill_names=("root-skill",),
                compiled_plans={"root-skill": plan},
            )

        self.assertIn("delegate_task", exposure.tools)
        self.assertIn("web_search", exposure.tools)
        for denied_tool in (
            "run_skill_process",
            "run_skill_script",
            "run_skill_python",
            "skill_http_get",
            "skill_http_post_json",
            "run_declared_command",
        ):
            self.assertNotIn(denied_tool, exposure.tools)
        self.assertFalse(any(
            row[0] == "invalid-adapter"
            for row in exposure.allowed_skill_resources
        ))
        self.assertFalse(any(
            row[0] == "invalid-adapter"
            for row in exposure.allowed_skill_scripts
        ))
        self.assertFalse(any(
            row[0] == "invalid-adapter"
            for row in exposure.allowed_skill_commands
        ))
        self.assertFalse(any(
            row[0] == "invalid-adapter"
            for row in exposure.allowed_skill_http_prefixes
        ))
        self.assertFalse(any(
            row[0] == "invalid-adapter"
            for row in exposure.allowed_skill_http_post_prefixes
        ))
        self.assertFalse(any(
            row[0] == "invalid-adapter"
            for row in exposure.allowed_skill_package_digests
        ))

    def test_optional_gate_skill_failure_keeps_native_or_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "root-skill"
            skill_dir.mkdir(parents=True, exist_ok=True)
            gate = compile_symbolic_knowledge_gate(
                {
                    "checks": [{
                        "id": "coverage",
                        "question": "Is another evidence lookup required?",
                        "if_yes": {
                            "tool_groups": [{
                                "id": "evidence-source",
                                "any_of": [
                                    "skill:missing-adapter",
                                    "web_search",
                                ],
                            }],
                        },
                    }],
                },
                skill_dir=skill_dir,
                source_file="workers/research.yaml",
                worker_id="research",
            ).ir
            execution = {
                "workers": [{
                    "id": "research",
                    "file": "workers/research.yaml",
                    "tools": ["web_search"],
                    "knowledge_gate_ir": gate,
                    "knowledge_gate_skill_refs": ["missing-adapter"],
                }],
                "routes": [{
                    "id": "selected",
                    "patterns": ["ZEBRA"],
                    "requires_full_output": False,
                    "waves": [{
                        "id": "research-wave",
                        "mode": "sequential",
                        "workers": ["research"],
                        "dependencies": [],
                    }],
                }],
                "intent_classification": {"dimensions": []},
                "knowledge_bootstrap": {"sources": []},
                "diagnostics": {"errors": [], "warnings": []},
            }
            contract = {
                "worker_files": ["workers/research.yaml"],
                "workers": execution["workers"],
                "execution_contract": execution,
                "requires_worker_outputs": True,
            }
            root = _precompiled_package(
                skill_dir=skill_dir,
                linked_files={"workers": ["workers/research.yaml"]},
                workflow_contract=contract,
            )

            exposure = _bounded_skill_execution_exposure(
                "Produce a ZEBRA report",
                [
                    "skill_view",
                    "delegate_task",
                    "web_search",
                    "submit_knowledge_gate_decisions",
                ],
                {"root-skill"},
                {"root-skill": root},
                {"root-skill": ()},
                selected_skill_names=("root-skill",),
            )

        self.assertIn("delegate_task", exposure.tools)
        self.assertIn("web_search", exposure.tools)
        self.assertNotIn(
            "capability_skill_package_unavailable:missing-adapter",
            exposure.missing_requirements,
        )

    def test_dynamic_resource_allowance_tracks_monotonic_observed_union(self):
        skill_name = "dynamic-resource-skill"
        orchestrator = "orchestration/main.yaml"
        selected_paths = [f"references/selected-{index}.md" for index in range(8)]
        worker_paths = ["workers/route-a.yaml", "workers/route-b.yaml"]
        state = HarnessRunState(
            session_skill_names={skill_name},
            viewed_skill_names={skill_name},
            skill_workflow_contracts={
                skill_name: {
                    "orchestrator_files": [orchestrator],
                    "worker_files": worker_paths,
                },
            },
            skill_execution_plans={
                skill_name: {
                    "intent_classification": {
                        "dimensions": [{
                            "id": "scope",
                            "values": ["selected"],
                            "source_file": orchestrator,
                        }],
                    },
                    "workers": {
                        "route-a": {"file": worker_paths[0]},
                        "route-b": {"file": worker_paths[1]},
                    },
                    "required_workers": [],
                    "route": {},
                    "requires_full_output": False,
                },
            },
        )
        observed: set[tuple[str, str]] = set()
        inspection_limit, total_limit = (
            _refresh_skill_workflow_continuation_allowance(
                state, observed, 1, 65, 64,
            )
        )
        self.assertEqual(inspection_limit, 2)  # manifest + orchestrator
        self.assertEqual(total_limit, 66)

        state.skill_pending_intent_resource_closure[skill_name] = {
            "required_resources": selected_paths,
        }
        inspection_limit, total_limit = (
            _refresh_skill_workflow_continuation_allowance(
                state, observed, inspection_limit, total_limit, 64,
            )
        )
        self.assertEqual(inspection_limit, 10)
        self.assertEqual(total_limit, 74)

        # Closing intent removes the transient resource set and materializes a
        # disjoint route. The allowance must cover the complete observed union,
        # rather than shrink to the four resources in the current snapshot.
        state.skill_pending_intent_resource_closure.pop(skill_name)
        state.skill_completed_intent.add(skill_name)
        state.skill_execution_plans[skill_name]["required_workers"] = [
            "route-a", "route-b",
        ]
        inspection_limit, total_limit = (
            _refresh_skill_workflow_continuation_allowance(
                state, observed, inspection_limit, total_limit, 64,
            )
        )
        self.assertEqual(inspection_limit, 12)
        self.assertEqual(total_limit, 76)
        self.assertEqual(len(observed), 12)

        # Re-observing the same state cannot manufacture retry budget.
        for _ in range(20):
            repeated = _refresh_skill_workflow_continuation_allowance(
                state, observed, inspection_limit, total_limit, 64,
            )
            self.assertEqual(repeated, (inspection_limit, total_limit))
            self.assertEqual(len(observed), 12)

        # Even sequentially appearing compiled resources remain hard bounded.
        for batch in range(3):
            state.skill_pending_intent_resource_closure[skill_name] = {
                "required_resources": [
                    f"references/batch-{batch}-{index}.md"
                    for index in range(100)
                ],
            }
            inspection_limit, total_limit = (
                _refresh_skill_workflow_continuation_allowance(
                    state, observed, inspection_limit, total_limit, 64,
                )
            )
        self.assertEqual(inspection_limit, 256)
        self.assertEqual(total_limit, 320)

    def test_two_stage_compiled_resource_union_sets_finite_dynamic_allowance(self):
        skill_name = "two-stage-skill"
        intent_paths = [f"intent/dimension-{index}.yaml" for index in range(1, 32)]
        worker_paths = [f"workers/worker-{index}.yaml" for index in range(1, 32)]
        orchestrator = "orchestration/main.yaml"
        state = HarnessRunState(
            session_skill_names={skill_name},
            viewed_skill_names={skill_name},
            skill_workflow_contracts={
                skill_name: {
                    "orchestrator_files": [orchestrator],
                    "worker_files": worker_paths,
                },
            },
            skill_execution_plans={
                skill_name: {
                    "intent_classification": {
                        "dimensions": [
                            {
                                "id": f"dimension-{index}",
                                "values": ["selected"],
                                "source_file": path,
                            }
                            for index, path in enumerate(intent_paths, 1)
                        ],
                    },
                    "workers": {
                        f"worker-{index}": {
                            "file": path,
                        }
                        for index, path in enumerate(worker_paths, 1)
                    },
                    "required_workers": [
                        f"worker-{index}" for index in range(1, 32)
                    ],
                    "route": {},
                    "requires_full_output": False,
                },
            },
        )

        # manifest + (orchestrator + 31 intent files) + 31 route workers.
        # The shared orchestrator is counted only once across both phases.
        self.assertEqual(
            _compiled_skill_inspection_auto_call_limit(state),
            64,
        )

        # The allowance scales for legitimate compiled multi-Skill plans but
        # remains globally finite even if many plans declare the maximum set.
        contract = state.skill_workflow_contracts[skill_name]
        plan = state.skill_execution_plans[skill_name]
        for index in range(2, 6):
            additional = f"two-stage-skill-{index}"
            state.session_skill_names.add(additional)
            state.viewed_skill_names.add(additional)
            state.skill_workflow_contracts[additional] = contract
            state.skill_execution_plans[additional] = plan
        self.assertEqual(
            _compiled_skill_inspection_auto_call_limit(state),
            256,
        )

    def test_exact_skill_view_policy_rejects_name_path_or_shape_changes(self):
        policy = {
            "tools": ["skill_view"],
            "max_calls": 1,
            "expected_skill_view": {
                "name": "compiled-skill",
                "file_path": "workers/worker-1.yaml",
            },
        }
        exact = {
            "name": "compiled-skill",
            "file_path": "workers/worker-1.yaml",
        }
        self.assertEqual(
            "",
            _workflow_gate_call_error(
                policy, "skill_view", exact, prior_call_count=0,
            ),
        )
        for changed in (
            {**exact, "name": "other-skill"},
            {**exact, "file_path": "workers/worker-1.yaml r"},
            {**exact, "file_path": "../worker-1.yaml"},
            {**exact, "extra": "model-added"},
        ):
            with self.subTest(changed=changed):
                self.assertIn(
                    "exact compiled skill_view arguments",
                    _workflow_gate_call_error(
                        policy,
                        "skill_view",
                        changed,
                        prior_call_count=0,
                    ),
                )

        paged_policy = {
            **policy,
            "expected_skill_view": {**exact, "offset": 100_000},
        }
        self.assertEqual(
            "",
            _workflow_gate_call_error(
                paged_policy,
                "skill_view",
                {**exact, "offset": 100_000},
                prior_call_count=0,
            ),
        )
        self.assertIn(
            "exact compiled skill_view arguments",
            _workflow_gate_call_error(
                paged_policy,
                "skill_view",
                {**exact, "offset": 100_001},
                prior_call_count=0,
            ),
        )

    def test_unsafe_compiled_resource_fails_resolution_closed(self):
        state = HarnessRunState(
            session_skill_names={"compiled-skill"},
            viewed_skill_names={"compiled-skill"},
            skill_available_categories={"compiled-skill": {"workers"}},
            skill_category_files={
                "compiled-skill": {"workers": ["workers/../../outside.yaml"]},
            },
            skill_workflow_contracts={
                "compiled-skill": {"worker_files": ["workers/../../outside.yaml"]},
            },
        )
        state.viewed_skill_files["compiled-skill"] = {"__manifest__"}
        target, error = _compiled_skill_inspection_target(
            state,
            "inspect explicit workflow resources for session skill "
            "'compiled-skill' "
            "(pending 1 of 1 declared-resource inspection receipts)",
        )
        self.assertIsNone(target)
        self.assertIn("unsafe compiled resource path", error)


class DeterministicSkillInspectionRunTests(unittest.IsolatedAsyncioTestCase):
    provider = {
        "id": "mock-skill-inspection",
        "base_url": "http://model.invalid/v1",
        "api_model": "mock-skill-inspection",
        "api_key": "EMPTY",
        "protocol": "openai",
        "provider": "mock",
        "context_length": 64_000,
        "is_multimodal": True,
    }

    async def test_standard_instruction_only_skill_finishes_without_invented_dag(self):
        skill_name = "portable-instruction-skill"
        responses = [
            _tool_call_response(
                "view-standard-skill",
                "skill_view",
                {"name": skill_name},
            ),
            _stop_response("已按该 Skill 的正文指令完成。"),
        ]
        request_bodies: list[dict] = []
        dispatches: list[tuple[str, dict]] = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                request_bodies.append(kwargs.get("json"))
                return _FakeResponse(responses.pop(0))

        async def fake_dispatch(name, args, *, context):
            dispatches.append((name, dict(args)))
            return json.dumps({
                "name": skill_name,
                "skill_md_sha256": _SKILL_MD_SHA256,
                "description": "A standard portable instruction-only Skill.",
                "content": "Answer once, cite the supplied input, and do not delegate.",
                "frontmatter": {
                    "name": skill_name,
                    "description": "A standard portable instruction-only Skill.",
                    "allowed-tools": "skill_view",
                },
                "package_diagnostics": {
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                },
            }, ensure_ascii=False)

        schemas = [{
            "type": "function",
            "function": {
                "name": "skill_view",
                "description": "skill_view",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch(
                    "skills.loader.load_skill_content",
                    return_value=_precompiled_package(
                        skill_dir=Path(temp_dir) / "skill-package",
                    ),
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[{"name": skill_name, "scope": "session"}],
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-skill-inspection",
                        [{"role": "user", "content": f"请运行 {skill_name} 并回答结果"}],
                        ["skill_view"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-standard-skill",
                        session_id="s-standard-skill",
                        max_iterations=4,
                    )
                ]

        self.assertFalse(responses)
        self.assertEqual(2, len(request_bodies))
        self.assertEqual([("skill_view", {"name": skill_name})], dispatches)
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def _run(self, *, fail_path: str | None = None):
        skill_name = "generic-auto-skill"
        worker_paths = [f"workers/worker-{index}.yaml" for index in range(1, 9)]
        responses = [
            _tool_call_response("main-skill", "skill_view", {"name": skill_name}),
        ]
        if fail_path is None:
            responses.append(_stop_response())
        request_bodies: list[dict] = []
        dispatches: list[tuple[str, dict, int]] = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                request_bodies.append(kwargs.get("json"))
                return _FakeResponse(responses.pop(0))

        async def fake_dispatch(name, args, *, context):
            dispatches.append((name, dict(args), len(request_bodies)))
            if fail_path is not None and args.get("file_path") == fail_path:
                return json.dumps({
                    "status": "error",
                    "error": "synthetic compiled resource read failure",
                })
            if not args.get("file_path"):
                return json.dumps({
                    "name": skill_name,
                    "skill_md_sha256": _SKILL_MD_SHA256,
                    "linked_files": {"workers": worker_paths},
                    "resource_graph": {
                        "categories": {
                            "workers": {"sample": worker_paths},
                        },
                        "suggested_files": worker_paths,
                    },
                    "workflow_contract": {
                        "worker_files": worker_paths,
                        "requires_worker_outputs": False,
                    },
                })
            return json.dumps({
                "name": skill_name,
                "file_path": args["file_path"],
                "content": "compiled worker contract",
            })

        schemas = [{
            "type": "function",
            "function": {
                "name": "skill_view",
                "description": "skill_view",
                "parameters": {"type": "object", "properties": {}},
            },
        }]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch(
                    "skills.loader.load_skill_content",
                    return_value=_precompiled_package(
                        skill_dir=Path(temp_dir) / "skill-package",
                        linked_files={"workers": worker_paths},
                        workflow_contract={
                            "worker_files": worker_paths,
                            "requires_worker_outputs": False,
                        },
                    ),
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[{"name": skill_name, "scope": "session"}],
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-skill-inspection",
                        [{"role": "user", "content": f"请运行 {skill_name}"}],
                        ["skill_view"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-deterministic-inspection",
                        session_id="s-deterministic-inspection",
                        max_iterations=8,
                    )
                ]
        return worker_paths, request_bodies, dispatches, events, responses

    async def test_eight_compiled_worker_reads_are_auto_dispatched_before_next_llm(self):
        worker_paths, request_bodies, dispatches, events, responses = await self._run()

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 2)
        self.assertEqual(
            [args.get("file_path") for _, args, _ in dispatches[1:]],
            worker_paths,
        )
        self.assertTrue(all(request_count == 1 for _, _, request_count in dispatches[1:]))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

        # Only the model-authored main Skill selection appears as a native tool
        # call in model history. Exact compiled paths are local harness reads.
        assistant_calls = [
            call
            for message in request_bodies[-1]["messages"]
            if message.get("role") == "assistant"
            for call in (message.get("tool_calls") or [])
        ]
        self.assertEqual(len(assistant_calls), 1)
        self.assertEqual(
            json.loads(assistant_calls[0]["function"]["arguments"]),
            {"name": "generic-auto-skill"},
        )
        auto_starts = [
            event for event in events
            if event.get("event_type") == "tool.started"
            and event.get("payload", {}).get("workflow_auto_dispatch") is True
        ]
        self.assertEqual(len(auto_starts), 8)

    async def test_large_workflow_resource_auto_reads_every_page_before_next_llm(self):
        skill_name = "generic-paged-workflow"
        worker_path = "workers/large-worker.yaml"
        digest = "b" * 64
        responses = [
            _tool_call_response("main-paged-skill", "skill_view", {"name": skill_name}),
            _stop_response("completed after exact paginated inspection"),
        ]
        request_bodies: list[dict] = []
        dispatches: list[tuple[str, dict, int]] = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                request_bodies.append(kwargs.get("json"))
                return _FakeResponse(responses.pop(0))

        async def fake_dispatch(name, args, *, context):
            dispatches.append((name, dict(args), len(request_bodies)))
            if not args.get("file_path"):
                return json.dumps({
                    "name": skill_name,
                    "skill_md_sha256": _SKILL_MD_SHA256,
                    "linked_files": {"workers": [worker_path]},
                    "resource_graph": {
                        "categories": {"workers": {"sample": [worker_path]}},
                    },
                    "workflow_contract": {
                        "worker_files": [worker_path],
                        "requires_worker_outputs": False,
                    },
                })
            offset = int(args.get("offset", 0))
            page_sizes = {0: 100_000, 100_000: 100_000, 200_000: 25_000}
            returned = page_sizes[offset]
            next_offset = offset + returned
            has_more = next_offset < 225_000
            return json.dumps({
                "success": True,
                "name": skill_name,
                "file": worker_path,
                "content": "x" * returned,
                "sha256": digest,
                "truncated": has_more,
                "pagination": {
                    "unit": "unicode_codepoints",
                    "offset": offset,
                    "limit": 100_000,
                    "returned_chars": returned,
                    "total_chars": 225_000,
                    "has_more": has_more,
                    "next_offset": next_offset if has_more else None,
                },
            })

        schemas = [{
            "type": "function",
            "function": {
                "name": "skill_view",
                "description": "skill_view",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch(
                    "skills.loader.load_skill_content",
                    return_value=_precompiled_package(
                        skill_dir=Path(temp_dir) / "skill-package",
                        linked_files={"workers": [worker_path]},
                        workflow_contract={
                            "worker_files": [worker_path],
                            "requires_worker_outputs": False,
                        },
                    ),
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[{"name": skill_name, "scope": "session"}],
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-skill-inspection",
                        [{"role": "user", "content": f"请运行 {skill_name}"}],
                        ["skill_view"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-paged-inspection",
                        session_id="s-paged-inspection",
                        max_iterations=4,
                    )
                ]

        paged_args = [
            args for _, args, _ in dispatches if args.get("file_path") == worker_path
        ]
        self.assertEqual(
            paged_args,
            [
                {"name": skill_name, "file_path": worker_path},
                {"name": skill_name, "file_path": worker_path, "offset": 100_000},
                {"name": skill_name, "file_path": worker_path, "offset": 200_000},
            ],
        )
        self.assertTrue(all(count == 1 for _, _, count in dispatches[1:]))
        self.assertEqual(len(request_bodies), 2)
        self.assertFalse(responses)
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_failed_auto_read_terminates_without_retry_or_second_llm(self):
        fail_path = "workers/worker-1.yaml"
        _, request_bodies, dispatches, events, responses = await self._run(
            fail_path=fail_path,
        )

        self.assertFalse(responses)
        self.assertEqual(len(request_bodies), 1)
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(dispatches[-1][1]["file_path"], fail_path)
        self.assertEqual(events[-1]["type"], "error")
        failed = [
            event for event in events
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(
            failed[0]["payload"]["finish_reason"],
            "skill_inspection_failed",
        )

    async def test_invalid_worker_result_schema_fails_before_delegate_dispatch(self):
        skill_name = "schema-fail-skill"
        worker_id = "worker-a"
        worker_path = "workers/worker-a.yaml"
        workers = [{
            "id": worker_id,
            "file": worker_path,
            "tools": ["read_file"],
            "output_schema": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "format": "email"},
                },
                "required": ["email"],
            },
        }]
        execution = {
            "workers": workers,
            "routes": [{
                "id": "selected-route",
                "patterns": ["schema-fail-skill"],
                "requires_full_output": False,
                "waves": [{
                    "id": "worker-wave",
                    "mode": "parallel",
                    "workers": [worker_id],
                    "dependencies": [],
                }],
            }],
            "intent_classification": {"dimensions": []},
            "knowledge_bootstrap": {"sources": []},
            "diagnostics": {"errors": [], "warnings": []},
        }
        contract = {
            "worker_files": [worker_path],
            "workers": workers,
            "execution_contract": execution,
            "requires_worker_outputs": True,
        }
        responses = [
            _tool_call_response(
                "main-schema-fail",
                "skill_view",
                {"name": skill_name},
            ),
        ]
        dispatches: list[tuple[str, dict]] = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                return _FakeResponse(responses.pop(0))

        async def fake_dispatch(name, args, *, context):
            dispatches.append((name, dict(args)))
            if name == "delegate_task":
                self.fail("invalid result schema must fail before delegation")
            if not args.get("file_path"):
                return json.dumps({
                    "name": skill_name,
                    "skill_md_sha256": _SKILL_MD_SHA256,
                    "linked_files": {"workers": [worker_path]},
                    "resource_graph": {
                        "categories": {
                            "workers": {"sample": [worker_path]},
                        },
                    },
                    "workflow_contract": contract,
                })
            return json.dumps({
                "name": skill_name,
                "file_path": worker_path,
                "content": "compiled worker contract",
            })

        schemas = [{
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        } for name in ("skill_view", "delegate_task", "read_file")]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch(
                    "skills.loader.load_skill_content",
                    return_value=_precompiled_package(
                        skill_dir=Path(temp_dir) / "skill-package",
                        linked_files={"workers": [worker_path]},
                        workflow_contract=contract,
                    ),
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[{"name": skill_name, "scope": "session"}],
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-skill-inspection",
                        [{"role": "user", "content": f"请运行 {skill_name}"}],
                        ["skill_view", "delegate_task", "read_file"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-schema-fail",
                        session_id="s-schema-fail",
                        max_iterations=4,
                    )
                ]

        self.assertFalse(responses)
        self.assertNotIn("delegate_task", [name for name, _ in dispatches])
        failed = [
            event for event in events
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(1, len(failed))
        self.assertEqual(
            "skill_result_contract_invalid",
            failed[0]["payload"]["finish_reason"],
        )
        self.assertFalse(failed[0]["payload"]["actual_dispatch_attempted"])
        self.assertIn("format", failed[0]["payload"]["error"])
        diagnostic = failed[0]["payload"]["contract_diagnostic"]
        self.assertEqual(worker_id, diagnostic["step_id"])
        self.assertFalse(diagnostic["actual_dispatch_attempted"])
        self.assertEqual("error", events[-1]["type"])

    async def _capture_descriptor_contract_dispatch(
        self,
        *,
        skill_name: str,
        execution: dict,
    ) -> tuple[list[dict], list[dict]]:
        workers = list(execution.get("workers") or [])
        worker_files = [
            str(worker["file"])
            for worker in workers
            if isinstance(worker, dict) and worker.get("file")
        ]
        linked_files = {"workers": worker_files} if worker_files else {}
        contract = {
            "worker_files": worker_files,
            "workers": workers,
            "execution_contract": execution,
            "requires_worker_outputs": bool(workers),
        }
        responses = [
            _tool_call_response(
                "activate-descriptor-contract",
                "skill_view",
                {"name": skill_name},
            ),
        ]
        delegated_tasks: list[dict] = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                return _FakeResponse(responses.pop(0))

        async def fake_dispatch(name, args, *, context):
            if name == "skill_view" and not args.get("file_path"):
                return json.dumps({
                    "name": skill_name,
                    "skill_md_sha256": _SKILL_MD_SHA256,
                    "linked_files": linked_files,
                    "resource_graph": {
                        "categories": (
                            {"workers": {"sample": worker_files}}
                            if worker_files else {}
                        ),
                    },
                    "workflow_contract": contract,
                })
            if name == "skill_view":
                return json.dumps({
                    "name": skill_name,
                    "file_path": args["file_path"],
                    "content": "portable compiled worker instructions",
                })
            tasks = (
                [dict(args)] if str(args.get("goal") or "").strip()
                else [dict(task) for task in (args.get("tasks") or [])]
            )
            delegated_tasks.extend(tasks)
            return json.dumps({
                "status": "error",
                "error": "bounded synthetic stop after metadata capture",
                "results": [
                    {
                        "status": "error",
                        "skill_name": task.get("skill_name"),
                        "step_type": task.get("step_type"),
                        "step_id": task.get("step_id"),
                        "worker_id": task.get("worker_id"),
                        "error": "bounded synthetic stop after metadata capture",
                        "terminal_reason": "synthetic_stop",
                        "failure_class": "test_stop",
                        "retryable": False,
                    }
                    for task in tasks
                ],
            })

        schemas = [{
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        } for name in ("skill_view", "delegate_task", "read_file")]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch(
                    "skills.loader.load_skill_content",
                    return_value=_precompiled_package(
                        skill_dir=Path(temp_dir) / "skill-package",
                        linked_files=linked_files,
                        workflow_contract=contract,
                    ),
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[{"name": skill_name, "scope": "session"}],
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-skill-inspection",
                        [{"role": "user", "content": f"请运行 {skill_name}"}],
                        ["skill_view", "delegate_task", "read_file"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-descriptor-contract",
                        session_id="s-descriptor-contract",
                        max_iterations=8,
                    )
                ]

        self.assertFalse(responses)
        return delegated_tasks, events

    async def test_descriptor_schemas_reach_cross_domain_bootstrap_and_worker_dispatch(self):
        catalog_skill = "software-catalog-portable"
        catalog_tasks, _ = await self._capture_descriptor_contract_dispatch(
            skill_name=catalog_skill,
            execution={
                "workers": [{
                    "id": "catalog-worker",
                    "file": "workers/catalog.yaml",
                    "tools": [{"name": "read_file"}],
                }],
                "routes": [{
                    "id": "catalog-route",
                    "patterns": [catalog_skill],
                    "requires_full_output": False,
                    "waves": [{
                        "id": "catalog-wave",
                        "mode": "parallel",
                        "workers": ["catalog-worker"],
                        "dependencies": [],
                    }],
                }],
                "intent_classification": {"dimensions": []},
                "knowledge_bootstrap": {"sources": [{
                    "id": "package-index",
                    "tool": "read_file",
                    "extract_fields": [
                        {
                            "field": "package_name",
                            "description": "Canonical package name",
                        },
                        {
                            "field": "versions",
                            "type": "array",
                            "items": {"type": "string"},
                            "label": "Published versions",
                        },
                    ],
                }]},
                "diagnostics": {"errors": [], "warnings": []},
            },
        )
        self.assertEqual(1, len(catalog_tasks))
        self.assertEqual("knowledge_bootstrap", catalog_tasks[0]["step_type"])
        self.assertEqual(
            ["package_name", "versions"],
            catalog_tasks[0]["required_result_fields"],
        )
        self.assertEqual(
            {
                "package_name": {},
                "versions": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            catalog_tasks[0]["required_result_schema"],
        )

        analytics_skill = "warehouse-analytics-portable"
        analytics_tasks, _ = await self._capture_descriptor_contract_dispatch(
            skill_name=analytics_skill,
            execution={
                "workers": [{
                    "id": "inventory-worker",
                    "file": "workers/inventory.yaml",
                    "tools": ["read_file"],
                    "output_schema": [
                        {
                            "name": "summary",
                            "description": "Human-readable inventory summary",
                        },
                        {
                            "name": "bins",
                            "schema": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "sku": {"type": "string"},
                                        "quantity": {"type": "integer"},
                                    },
                                    "required": ["sku", "quantity"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                    ],
                }],
                "routes": [{
                    "id": "inventory-route",
                    "patterns": [analytics_skill],
                    "requires_full_output": False,
                    "waves": [{
                        "id": "inventory-wave",
                        "mode": "parallel",
                        "workers": ["inventory-worker"],
                        "dependencies": [],
                    }],
                }],
                "intent_classification": {"dimensions": []},
                "knowledge_bootstrap": {"sources": []},
                "diagnostics": {"errors": [], "warnings": []},
            },
        )
        self.assertEqual(1, len(analytics_tasks))
        self.assertEqual("worker", analytics_tasks[0]["step_type"])
        self.assertEqual(
            ["summary", "bins"],
            analytics_tasks[0]["required_result_fields"],
        )
        self.assertEqual({}, analytics_tasks[0]["required_result_schema"]["summary"])
        self.assertEqual(
            "array",
            analytics_tasks[0]["required_result_schema"]["bins"]["type"],
        )

    async def test_selected_snapshot_failures_are_terminal_before_dispatch(self):
        skill_name = "snapshot-failure-portable"

        class NoModelClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                raise AssertionError(
                    "an invalid selected snapshot must fail before model IO"
                )

        schemas = [{
            "type": "function",
            "function": {
                "name": "skill_view",
                "description": "skill_view",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        for case in ("identity_missing", "script_inventory_failed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                skill_dir = Path(temp_dir) / "skill-package"
                valid_package = _precompiled_package(skill_dir=skill_dir)
                loaded_package = (
                    {
                        **valid_package,
                        "skill_dir": "",
                    }
                    if case == "identity_missing"
                    else valid_package
                )
                scanner_side_effect = (
                    RuntimeError("raw scanner detail")
                    if case == "script_inventory_failed"
                    else None
                )
                record = {
                    "name": skill_name,
                    "description": "Portable failure fixture.",
                    "scope": "session",
                    "path": str(skill_dir / "SKILL.md"),
                    "skill_dir": str(skill_dir),
                }
                with (
                    patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                    patch("agent_loop.httpx.AsyncClient", NoModelClient),
                    patch("agent_loop.get_schemas", return_value=schemas),
                    patch(
                        "agent_loop.build_system_prompt",
                        return_value="system",
                    ),
                    patch(
                        "agent_loop.load_workspace_context",
                        return_value="",
                    ),
                    patch(
                        "agent_loop._fetch_goal",
                        AsyncMock(return_value=None),
                    ),
                    patch(
                        "skills.loader.load_skill_content",
                        return_value=loaded_package,
                    ),
                    patch(
                        "skills.scanner.find_all_skills",
                        return_value=[record],
                    ),
                    patch(
                        "skills.scanner.skill_runnable_script_resources",
                        side_effect=scanner_side_effect,
                        return_value=(),
                    ),
                ):
                    events = [
                        event
                        async for event in run_stream(
                            "mock-skill-inspection",
                            [{
                                "role": "user",
                                "content": (
                                    f"Use {skill_name} to produce a "
                                    "comprehensive evidence report."
                                ),
                            }],
                            ["skill_view"],
                            provider_override=self.provider,
                            allow_session_mcp=False,
                            user_id="u-snapshot-failure",
                            session_id=f"s-{case}",
                            max_iterations=2,
                        )
                    ]

                failed = [
                    event for event in events
                    if event.get("event_type") == "run.failed"
                ]
                self.assertEqual(1, len(failed))
                self.assertEqual(
                    "skill_package_snapshot_invalid",
                    failed[0]["payload"]["finish_reason"],
                )
                diagnostic = failed[0]["payload"]["contract_diagnostic"]
                self.assertTrue(
                    str(diagnostic["error_code"]).startswith(
                        "skill_"
                    )
                )
                self.assertNotIn(
                    "raw scanner detail",
                    json.dumps(events),
                )

    async def test_declared_route_snapshot_survives_bootstrap_into_kg_worker(self):
        """Regression for a non-explicit route reaching the KG worker phase.

        The selected Skill name never appears in the request.  Selection is
        therefore owned by its unique declared route, and the package snapshot
        bound at ingress must still be available after bootstrap when the
        worker's symbolic knowledge gate is compiled.
        """

        skill_name = "route-bound-portable-workflow"
        worker_id = "evidence-worker"
        worker_path = "workers/evidence.yaml"
        request = (
            "Produce a comprehensive multi-agent evidence report for "
            "the exact ZEBRA-ROUTE readiness scenario."
        )
        delegated_tasks: list[dict] = []
        dispatches: list[tuple[str, dict]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "skill-package"
            skill_dir.mkdir(parents=True, exist_ok=True)
            symbolic_gate = compile_symbolic_knowledge_gate(
                {
                    "checks": [{
                        "id": "source-ready",
                        "question": "Is the prerequisite source ready?",
                    }],
                },
                skill_dir=skill_dir,
                source_file=worker_path,
                worker_id=worker_id,
            ).ir
            execution = {
                "workers": [{
                    "id": worker_id,
                    "file": worker_path,
                    "tools": [{"name": "read_file"}],
                    "knowledge_gate_ir": symbolic_gate,
                    "required_gate_ids": ["source-ready"],
                }],
                "routes": [{
                    "id": "zebra-readiness",
                    "patterns": ["ZEBRA-ROUTE"],
                    "requires_full_output": False,
                    "waves": [{
                        "id": "evidence-wave",
                        "mode": "sequential",
                        "workers": [worker_id],
                        "dependencies": [],
                    }],
                }],
                "intent_classification": {"dimensions": []},
                "knowledge_bootstrap": {"sources": [{
                    "id": "baseline-source",
                    "tool": "read_file",
                    "extract_fields": [{
                        "field": "baseline",
                        "description": "Bounded prerequisite evidence",
                    }],
                }]},
                "diagnostics": {"errors": [], "warnings": []},
            }
            contract = {
                "worker_files": [worker_path],
                "workers": execution["workers"],
                "execution_contract": execution,
                "requires_worker_outputs": True,
            }
            package = _precompiled_package(
                skill_dir=skill_dir,
                linked_files={"workers": [worker_path]},
                workflow_contract=contract,
            )
            record = {
                "name": skill_name,
                "description": "Unrelated portable orchestration fixture.",
                "scope": "session",
                "path": str(skill_dir / "SKILL.md"),
                "skill_dir": str(skill_dir),
            }

            class NoModelClient:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def stream(self, method, url, **kwargs):
                    raise AssertionError(
                        "declared workflow prerequisites must auto-dispatch"
                    )

            async def fake_dispatch(name, args, *, context):
                dispatches.append((name, dict(args)))
                if name == "skill_view" and not args.get("file_path"):
                    return json.dumps({
                        "name": skill_name,
                        "skill_md_sha256": _SKILL_MD_SHA256,
                        "linked_files": {"workers": [worker_path]},
                        "resource_graph": {
                            "categories": {
                                "workers": {"sample": [worker_path]},
                            },
                        },
                        "workflow_contract": contract,
                    })
                if name == "skill_view":
                    return json.dumps({
                        "name": skill_name,
                        "file_path": args["file_path"],
                        "content": "bounded worker instructions",
                    })
                self.assertEqual("delegate_task", name)
                tasks = (
                    [dict(args)]
                    if str(args.get("goal") or "").strip()
                    else [dict(task) for task in (args.get("tasks") or [])]
                )
                delegated_tasks.extend(tasks)
                if tasks[0]["step_type"] == "knowledge_bootstrap":
                    return json.dumps({
                        "status": "completed",
                        "task_count": 1,
                        "completed_count": 1,
                        "results": [{
                            "status": "completed",
                            "skill_name": skill_name,
                            "step_type": "knowledge_bootstrap",
                            "step_id": "baseline-source",
                            "result_path": "results/baseline.md",
                            "result_chars": 256,
                            "summary": "baseline — PASS",
                        }],
                    })
                return json.dumps({
                    "status": "error",
                    "task_count": 1,
                    "results": [{
                        "status": "error",
                        "skill_name": skill_name,
                        "step_type": "worker",
                        "step_id": worker_id,
                        "worker_id": worker_id,
                        "error": "bounded synthetic stop after KG capture",
                        "terminal_reason": "synthetic_stop",
                        "failure_class": "test_stop",
                        "retryable": False,
                    }],
                })

            schemas = [{
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            } for name in ("skill_view", "delegate_task", "read_file")]

            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", NoModelClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch(
                    "skills.loader.load_skill_content",
                    return_value=package,
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[record],
                ),
                patch(
                    "skills.scanner.skill_runnable_script_resources",
                    return_value=(),
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-skill-inspection",
                        [{"role": "user", "content": request}],
                        ["skill_view", "delegate_task", "read_file"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-declared-route-kg",
                        session_id="s-declared-route-kg",
                        max_iterations=8,
                    )
                ]

        self.assertEqual(
            ["knowledge_bootstrap", "worker"],
            [task["step_type"] for task in delegated_tasks],
        )
        worker_task = delegated_tasks[-1]
        self.assertEqual(worker_id, worker_task["worker_id"])
        self.assertEqual(
            "source-ready",
            worker_task["knowledge_gate_plan"]["checks"][0]["id"],
        )
        self.assertRegex(
            worker_task["knowledge_gate_plan_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertIn(
            "submit_knowledge_gate_decisions",
            worker_task["tools"],
        )
        self.assertFalse(any(
            event.get("payload", {}).get("exception_class") == "NameError"
            for event in events
            if isinstance(event, dict)
        ))

    async def test_disjoint_intent_and_route_resources_both_auto_dispatch(self):
        skill_name = "two-stage-auto-skill"
        orchestrator = "orchestration/main.yaml"
        intent_paths = [f"intent/dimension-{index}.yaml" for index in range(1, 32)]
        worker_paths = [f"workers/worker-{index}.yaml" for index in range(1, 32)]
        worker_ids = [f"worker-{index}" for index in range(1, 32)]
        dimensions = [
            {
                "id": f"dimension-{index}",
                "required": True,
                "values": ["selected"],
                "source_file": intent_paths[index - 1],
            }
            for index in range(1, 32)
        ]
        dimensions[0]["mappings"] = {
            "workers_map": {"selected": worker_ids},
        }
        workers = [
            {
                "id": worker_id,
                "file": worker_path,
                "tools": ["read_file"],
            }
            for worker_id, worker_path in zip(worker_ids, worker_paths)
        ]
        execution = {
            "workers": workers,
            "routes": [{
                "id": "full-route",
                "patterns": ["never-match-this-request"],
                "requires_full_output": False,
                "waves": [{
                    "id": "all-workers",
                    "mode": "parallel",
                    "workers": worker_ids,
                    "dependencies": [],
                }],
            }],
            "intent_classification": {"dimensions": dimensions},
            "knowledge_bootstrap": {"sources": []},
            "diagnostics": {"errors": [], "warnings": []},
        }
        contract = {
            "orchestrator_files": [orchestrator],
            "worker_files": worker_paths,
            "workers": workers,
            "execution_contract": execution,
            "requires_worker_outputs": True,
        }
        responses = [
            _tool_call_response(
                "main-two-stage",
                "skill_view",
                {"name": skill_name},
            ),
        ]
        request_bodies: list[dict] = []
        dispatches: list[tuple[str, dict]] = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                request_bodies.append(kwargs.get("json"))
                return _FakeResponse(responses.pop(0))

        async def fake_dispatch(name, args, *, context):
            dispatches.append((name, dict(args)))
            if name == "skill_view" and not args.get("file_path"):
                return json.dumps({
                    "name": skill_name,
                    "skill_md_sha256": _SKILL_MD_SHA256,
                    "linked_files": {
                        "orchestration": [orchestrator],
                        "workers": worker_paths,
                    },
                    "resource_graph": {
                        "categories": {
                            "orchestration": {"sample": [orchestrator]},
                            "workers": {"sample": worker_paths},
                        },
                    },
                    "workflow_contract": contract,
                })
            if name == "skill_view":
                return json.dumps({
                    "name": skill_name,
                    "file_path": args["file_path"],
                    "content": "compiled resource",
                })
            delegated = [args] if args.get("goal") else list(args.get("tasks") or [])
            if delegated and delegated[0].get("step_type") == "intent_classification":
                selections = {
                    f"dimension-{index}": "selected"
                    for index in range(1, 32)
                }
                summary = "\n".join([
                    *[
                        f"dimension-{index} — PASS: selected"
                        for index in range(1, 32)
                    ],
                    "intent-resource-resolution — PASS: no mapped local files",
                    "INTENT_SELECTIONS_JSON: " + json.dumps(selections),
                ])
                return json.dumps({
                    "status": "completed",
                    "results": [{
                        "status": "completed",
                        "skill_name": skill_name,
                        "step_type": "intent_classification",
                        "step_id": "intent-classification",
                        "result_path": "results/intent.md",
                        "result_chars": len(summary),
                        "summary": summary,
                    }],
                })
            return json.dumps({
                "status": "error",
                "results": [
                    {
                        "status": "error",
                        "skill_name": skill_name,
                        "step_type": "worker",
                        "step_id": task["step_id"],
                        "worker_id": task["worker_id"],
                        "error": "bounded synthetic stop after resource inspection",
                        "terminal_reason": "synthetic_stop",
                        "failure_class": "test_stop",
                        "retryable": False,
                    }
                    for task in delegated
                ],
            })

        schemas = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("skill_view", "delegate_task", "read_file")
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            selected_package = _precompiled_package(
                skill_dir=Path(temp_dir) / "skill-package",
                linked_files={
                    "orchestration": [orchestrator],
                    "intent": intent_paths,
                    "workers": worker_paths,
                },
                workflow_contract=contract,
            )
            unrelated_package = _precompiled_package(
                skill_dir=Path(temp_dir) / "unrelated-package",
            )
            package_loader = Mock(side_effect=lambda path, **_kwargs: (
                unrelated_package
                if "unrelated-package" in str(path)
                else selected_package
            ))
            script_inventory = Mock(return_value=())
            skill_records = [
                {
                    "name": skill_name,
                    "description": "Selected two-stage workflow.",
                    "scope": "session",
                    "path": str(
                        Path(selected_package["skill_dir"]) / "SKILL.md"
                    ),
                    "skill_dir": selected_package["skill_dir"],
                },
                {
                    "name": "unrelated-package",
                    "description": "Unrelated package that must remain unread.",
                    "scope": "session",
                    "path": str(
                        Path(unrelated_package["skill_dir"]) / "SKILL.md"
                    ),
                    "skill_dir": unrelated_package["skill_dir"],
                },
            ]
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch(
                    "skills.loader.load_skill_content",
                    package_loader,
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=skill_records,
                ),
                patch(
                    "skills.scanner.skill_runnable_script_resources",
                    script_inventory,
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-skill-inspection",
                        [{"role": "user", "content": f"请运行 {skill_name}"}],
                        ["skill_view", "delegate_task", "read_file"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-two-stage-inspection",
                        session_id="s-two-stage-inspection",
                        max_iterations=4,
                    )
                ]

        inspected_paths = [
            args["file_path"]
            for name, args in dispatches
            if name == "skill_view" and args.get("file_path")
        ]
        self.assertEqual(
            inspected_paths,
            [orchestrator, *intent_paths, *worker_paths],
        )
        self.assertEqual(len(inspected_paths), 63)
        self.assertEqual(len(request_bodies), 1)
        # Initial binding loads/scans only the exact selected package. The
        # intent-driven route recompilation must reuse that frozen snapshot,
        # not traverse or reload the unrelated session catalog.
        self.assertEqual(1, package_loader.call_count)
        self.assertEqual(1, script_inventory.call_count)
        self.assertNotIn(
            "unrelated-package",
            [str(call.args[0]) for call in package_loader.call_args_list],
        )
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn(
            f"{skill_name}/worker/{worker_ids[0]}",
            events[-1]["msg"],
        )

    async def test_transient_selected_resources_do_not_starve_resolved_route_reads(self):
        skill_name = "transient-intent-resource-skill"
        orchestrator = "orchestration/main.yaml"
        selected_paths = [f"references/selected-{index}.md" for index in range(8)]
        worker_paths = ["workers/route-a.yaml", "workers/route-b.yaml"]
        worker_ids = ["route-a", "route-b"]
        workers = [
            {"id": worker_id, "file": worker_path, "tools": ["read_file"]}
            for worker_id, worker_path in zip(worker_ids, worker_paths)
        ]
        execution = {
            "workers": workers,
            "routes": [],
            "intent_classification": {
                "dimensions": [{
                    "id": "scope",
                    "required": True,
                    "values": ["selected"],
                    "source_file": orchestrator,
                    "mappings": {
                        "workers_map": {"selected": worker_ids},
                        "resource_map": {"selected": selected_paths},
                    },
                }],
            },
            "knowledge_bootstrap": {"sources": []},
            "diagnostics": {"errors": [], "warnings": []},
        }
        contract = {
            "orchestrator_files": [orchestrator],
            "worker_files": worker_paths,
            "workers": workers,
            "execution_contract": execution,
            "requires_worker_outputs": True,
        }
        responses = [
            _tool_call_response(
                "main-transient-resources",
                "skill_view",
                {"name": skill_name},
            ),
        ]
        dispatches: list[tuple[str, dict]] = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                return _FakeResponse(responses.pop(0))

        async def fake_dispatch(name, args, *, context):
            dispatches.append((name, dict(args)))
            if name == "skill_view" and not args.get("file_path"):
                return json.dumps({
                    "name": skill_name,
                    "skill_md_sha256": _SKILL_MD_SHA256,
                    "linked_files": {
                        "orchestration": [orchestrator],
                        "workers": worker_paths,
                        "references": selected_paths,
                    },
                    "resource_graph": {
                        "categories": {
                            "orchestration": {"sample": [orchestrator]},
                            "workers": {"sample": worker_paths},
                            "references": {"sample": selected_paths},
                        },
                    },
                    "workflow_contract": contract,
                })
            if name == "skill_view":
                return json.dumps({
                    "name": skill_name,
                    "file_path": args["file_path"],
                    "content": "compiled resource",
                })
            delegated = [args] if args.get("goal") else list(args.get("tasks") or [])
            if delegated and delegated[0].get("step_type") == "intent_classification":
                return json.dumps({
                    "status": "completed",
                    "results": [{
                        "status": "completed",
                        "skill_name": skill_name,
                        "step_type": "intent_classification",
                        "step_id": "intent-classification",
                        "result_path": "results/intent.md",
                        "result_chars": 400,
                        "intent_selections": {"scope": "selected"},
                        "tool_audit": {"inspected_skill_files": []},
                    }],
                })
            return json.dumps({
                "status": "error",
                "results": [{
                    "status": "error",
                    "skill_name": skill_name,
                    "step_type": "worker",
                    "step_id": task["step_id"],
                    "worker_id": task["worker_id"],
                    "error": "bounded synthetic stop after route inspection",
                    "terminal_reason": "synthetic_stop",
                    "failure_class": "test_stop",
                    "retryable": False,
                } for task in delegated],
            })

        schemas = [{
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        } for name in ("skill_view", "delegate_task", "read_file")]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", return_value=schemas),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch(
                    "skills.loader.load_skill_content",
                    return_value=_precompiled_package(
                        skill_dir=Path(temp_dir) / "skill-package",
                        linked_files={
                            "orchestration": [orchestrator],
                            "references": selected_paths,
                            "workers": worker_paths,
                        },
                        workflow_contract=contract,
                    ),
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[{"name": skill_name, "scope": "session"}],
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-skill-inspection",
                        [{"role": "user", "content": f"请运行 {skill_name}"}],
                        ["skill_view", "delegate_task", "read_file"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-transient-inspection",
                        session_id="s-transient-inspection",
                        max_iterations=4,
                    )
                ]

        inspected_paths = [
            args["file_path"]
            for name, args in dispatches
            if name == "skill_view" and args.get("file_path")
        ]
        self.assertEqual(
            inspected_paths,
            [orchestrator, *selected_paths, *worker_paths],
        )
        self.assertEqual(len(inspected_paths), len(set(inspected_paths)))
        auto_inspections = [
            event for event in events
            if event.get("event_type") == "tool.started"
            and event.get("payload", {}).get("workflow_auto_dispatch") is True
            and event.get("payload", {}).get("tool_name") == "skill_view"
        ]
        self.assertEqual(len(auto_inspections), len(inspected_paths))
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn(
            f"{skill_name}/worker/{worker_ids[0]}",
            events[-1]["msg"],
        )


if __name__ == "__main__":
    unittest.main()
