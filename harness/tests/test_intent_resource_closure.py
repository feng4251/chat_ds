import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import (
    HarnessRunState,
    _compiled_skill_inspection_target,
    _intent_local_resource_paths,
    run_stream,
)


SKILL = "generic-intent-resources"


def _plan(mapping):
    return {
        "selection": "pending_intent",
        "intent_classification": {
            "dimensions": [{
                "id": "scope",
                "required": True,
                "values": list(mapping),
                "mappings": {"resource_map": mapping},
            }],
        },
        "workers": {},
        "declared_routes": [],
        "available_bootstrap_sources": [],
        "available_aggregation_steps": [],
    }


def _state(mapping, declared_files):
    state = HarnessRunState()
    state.session_skill_names.add(SKILL)
    state.viewed_skill_names.add(SKILL)
    state.viewed_skill_files[SKILL] = {"__manifest__"}
    state.skill_available_categories[SKILL] = {"references"}
    state.skill_category_files[SKILL] = {"references": list(declared_files)}
    state.skill_workflow_contracts[SKILL] = {
        "orchestrator_files": ["orchestration/main.yaml"],
    }
    state.skill_execution_plans[SKILL] = _plan(mapping)
    return state


def _record_intent(state, selected, *, inspected=()):
    return state.record_delegate_task(
        {
            "goal": "classify",
            "skill_name": SKILL,
            "step_type": "intent_classification",
            "step_id": "intent-classification",
        },
        {"results": [{
            "status": "completed",
            "skill_name": SKILL,
            "step_type": "intent_classification",
            "step_id": "intent-classification",
            "result_path": "results/intent.md",
            "result_chars": 400,
            "intent_selections": {"scope": selected},
            "tool_audit": {"inspected_skill_files": list(inspected)},
        }]},
    )


class IntentResourceClosureTests(unittest.TestCase):
    def test_single_selected_resource_is_parent_read_then_gate_completes(self):
        path = "references/single.md"
        state = _state({"single": path}, [path])

        update = _record_intent(state, "single")

        self.assertEqual(update["pending_step_ids"], ["intent-classification"])
        self.assertNotIn(SKILL, state.skill_completed_intent)
        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        self.assertIn("inspect selected intent resources", reason)
        target, error = _compiled_skill_inspection_target(state, reason)
        self.assertEqual(error, "")
        self.assertEqual(target, {"name": SKILL, "file_path": path})

        state.record_skill_view(
            {"name": SKILL, "file_path": path},
            {"success": True, "name": SKILL, "file": path, "content": "rules"},
        )

        self.assertIn(SKILL, state.skill_completed_intent)
        self.assertNotIn(SKILL, state.skill_pending_intent_resource_closure)
        closure = state.skill_intent_results[SKILL]["resource_closure"]
        self.assertEqual(closure["status"], "completed")
        self.assertEqual(closure["missing_resources"], [])
        self.assertEqual(
            closure["receipts"],
            [{
                "skill_name": SKILL,
                "path": path,
                "source": "parent_skill_view",
                "tool_name": "skill_view",
                "verified": True,
            }],
        )

    def test_list_mapping_reuses_child_audit_and_reads_only_missing_path(self):
        first = "references/first.yaml"
        second = "references/second.toml"
        state = _state({"many": [first, second]}, [first, second])

        _record_intent(state, "many", inspected=[first])

        pending = state.skill_pending_intent_resource_closure[SKILL]
        self.assertEqual(pending["required_resources"], [first, second])
        self.assertEqual(pending["missing_resources"], [second])
        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        target, error = _compiled_skill_inspection_target(state, reason)
        self.assertEqual(error, "")
        self.assertEqual(target, {"name": SKILL, "file_path": second})

        state.record_skill_view(
            {"name": SKILL, "file_path": second},
            {"success": True, "name": SKILL, "file": second, "content": "rules"},
        )
        receipts = state.skill_intent_results[SKILL]["resource_closure"]["receipts"]
        self.assertEqual(
            [(item["path"], item["source"]) for item in receipts],
            [(first, "delegate_tool_audit"), (second, "parent_skill_view")],
        )

    def test_external_skill_tool_and_web_references_are_not_local_reads(self):
        local = "references/local.md"
        mappings = {
            "scope.skill_map": [
                "Skill:external-database.md",
                "tool:catalog.json",
                "MCP:remote.yaml",
                "HTTPS://example.invalid/rules.md",
                "WebSearch",
                local,
            ],
        }
        self.assertEqual(_intent_local_resource_paths(mappings), [local])

        state = _state({"mixed": mappings["scope.skill_map"]}, [local])
        _record_intent(state, "mixed")
        self.assertEqual(
            state.skill_pending_intent_resource_closure[SKILL][
                "required_resources"
            ],
            [local],
        )

    def test_unsafe_selected_path_is_terminal_and_never_auto_dispatched(self):
        for unsafe in ("../escape.md", "/etc/passwd.md", "folder\\escape.md"):
            with self.subTest(path=unsafe):
                state = _state({"unsafe": unsafe}, [])
                update = _record_intent(state, "unsafe")
                self.assertEqual(
                    update["terminal_failed_step_ids"],
                    ["intent-classification"],
                )
                self.assertNotIn(SKILL, state.skill_completed_intent)
                self.assertNotIn(SKILL, state.skill_pending_intent_resource_closure)
                self.assertIn("unsafe local Skill resource path", state.skill_failed_intent[SKILL])

    def test_nonexistent_safe_resource_stays_closed_until_skill_view_succeeds(self):
        missing = "references/not-present.md"
        state = _state({"missing": missing}, [])
        _record_intent(state, "missing")

        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        target, error = _compiled_skill_inspection_target(state, reason)
        self.assertEqual(error, "")
        self.assertEqual(target, {"name": SKILL, "file_path": missing})
        # A missing/failed skill_view is never recorded as a receipt. The gate
        # therefore remains closed and terminal_delegate_failure does not
        # misclassify this deterministic pending read as a child failure.
        self.assertNotIn(SKILL, state.skill_completed_intent)
        self.assertIsNone(state.terminal_delegate_failure())
        self.assertEqual(
            state.skill_intent_results[SKILL]["resource_closure"]["status"],
            "pending",
        )


def _tool_call_response(call_id, name, arguments):
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": call_id,
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }]},
                "finish_reason": None,
            }],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }),
        "data: [DONE]",
    ]


class _FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self.lines:
            yield line
            if isinstance(line, str) and line.startswith("data"):
                yield ""


class IntentResourceClosureRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonexistent_selected_resource_fails_closed_on_exact_skill(self):
        orchestrator = "orchestration/main.yaml"
        missing = "references/not-present.md"
        skill_digest = ""
        responses = [
            _tool_call_response("load-skill", "skill_view", {"name": SKILL}),
        ]
        dispatches = []
        execution = {
            "workers": [],
            "routes": [],
            "intent_classification": {
                "dimensions": [{
                    "id": "scope",
                    "required": True,
                    "values": ["missing"],
                    "mappings": {
                        "resource_map": {"missing": missing},
                    },
                    "source_file": orchestrator,
                }],
            },
            "knowledge_bootstrap": {"sources": []},
            "diagnostics": {"errors": [], "warnings": []},
        }
        contract = {
            "orchestrator_files": [orchestrator],
            "execution_contract": execution,
        }

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
                    "success": True,
                    "name": SKILL,
                    "skill_md_sha256": skill_digest,
                    "linked_files": {
                        "orchestration": [orchestrator],
                        "references": [],
                    },
                    "resource_graph": {
                        "categories": {
                            "orchestration": {"sample": [orchestrator]},
                            "references": {"sample": []},
                        },
                    },
                    "workflow_contract": contract,
                })
            if name == "skill_view" and args.get("file_path") == orchestrator:
                return json.dumps({
                    "success": True,
                    "name": SKILL,
                    "file": orchestrator,
                    "content": "intent contract",
                })
            if name == "delegate_task":
                return json.dumps({
                    "status": "completed",
                    "results": [{
                        "status": "completed",
                        "skill_name": SKILL,
                        "step_type": "intent_classification",
                        "step_id": "intent-classification",
                        "result_path": "results/intent.md",
                        "result_chars": 400,
                        "intent_selections": {"scope": "missing"},
                        "tool_audit": {"inspected_skill_files": []},
                    }],
                })
            if name == "skill_view" and args.get("file_path") == missing:
                return json.dumps({
                    "success": False,
                    "error": f"File '{missing}' not found in skill '{SKILL}'.",
                })
            raise AssertionError((name, args))

        schemas = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("skill_view", "delegate_task")
        ]
        provider = {
            "id": "mock-intent-closure",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-intent-closure",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": True,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "skill-package"
            orchestrator_file = skill_dir / orchestrator
            orchestrator_file.parent.mkdir(parents=True, exist_ok=True)
            skill_main = skill_dir / "SKILL.md"
            skill_main.write_text(
                "---\n"
                f"name: {SKILL}\n"
                "description: Exact intent-resource closure fixture.\n"
                "---\n"
                "# Intent Resource Closure\n",
                encoding="utf-8",
            )
            orchestrator_file.write_text(
                "intent contract\n",
                encoding="utf-8",
            )
            skill_digest = hashlib.sha256(skill_main.read_bytes()).hexdigest()
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
                    return_value={
                        "skill_dir": str(skill_dir),
                        "skill_md_sha256": skill_digest,
                        "linked_files": {
                            "orchestration": [orchestrator],
                            "references": [],
                        },
                        "workflow_contract": contract,
                        "package_diagnostics": {
                            "valid": True,
                            "errors": [],
                            "warnings": [],
                        },
                    },
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[{"name": SKILL, "scope": "session"}],
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-intent-closure",
                        [{
                            "role": "user",
                            "content": f"请严格运行 {SKILL} skill 生成完整报告",
                        }],
                        ["skill_view", "delegate_task"],
                        provider_override=provider,
                        allow_session_mcp=False,
                        user_id="u-intent-closure",
                        session_id="s-intent-closure",
                        max_iterations=4,
                    )
                ]

        self.assertFalse(responses)
        self.assertEqual(
            dispatches[-1],
            ("skill_view", {"name": SKILL, "file_path": missing}),
        )
        failed = [event for event in events if event.get("event_type") == "run.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["payload"]["finish_reason"], "skill_inspection_failed")
        self.assertEqual(events[-1]["type"], "error")


if __name__ == "__main__":
    unittest.main()
