from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import AsyncMock, patch

from agent_loop import (
    HarnessRunState,
    _artifact_payloads_from_tool_result,
    _bounded_skill_execution_exposure,
    _compiled_skill_inspection_target,
    _deterministic_verifier_payload,
    _prerequisite_result_paths,
)
from skills import manager as skill_manager
from skills import scanner
from skills.loader import load_skill_content
from tools import declared_command, path_security, skill_http, skill_script
from tools.context import ToolContext
from tools.delegation import (
    _exact_capability_skill_http_grants,
    _exact_declared_skill_command_grants,
    _exact_declared_skill_script_grants,
)
from tools.registry import dispatch
import workspace_context


MAIN_SKILL = "warehouse-reconcile"
CAPABILITY_SKILL = "inventory-api"


class _FakeHttpContent:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, _size: int):
        yield self._body


class _FakeHttpResponse:
    status = 200
    headers = {"Content-Type": "application/json; charset=utf-8"}
    charset = "utf-8"

    def __init__(self, body: bytes):
        self.content = _FakeHttpContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeHttpSession:
    calls: list[str] = []

    def __init__(self, **kwargs):
        self.connector = kwargs.get("connector")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        if self.connector is not None:
            closed = self.connector.close()
            if inspect.isawaitable(closed):
                await closed
        return False

    def get(self, url: str, **_kwargs):
        self.calls.append(url)
        return _FakeHttpResponse(b'{"warehouse":"W-7","count":2}')


class CrossDomainSkillPipelineTests(unittest.IsolatedAsyncioTestCase):
    """One non-clinical package crossing the compiler/runtime/tool boundary."""

    @staticmethod
    def _write(root: Path, relative_path: str, content: str) -> None:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def _install_fixture(self, skills_base: Path, user_id: str, session_id: str) -> None:
        session_root = skills_base / user_id / session_id
        main = session_root / MAIN_SKILL
        capability = session_root / CAPABILITY_SKILL

        self._write(
            main,
            "SKILL.md",
            """
            ---
            name: warehouse-reconcile
            version: "1.0"
            description: Reconcile warehouse inventory into a closed data package.
            ---
            # Warehouse reconciliation

            Follow the declared workflow and produce only its declared artifacts.
            """,
        )
        self._write(
            main,
            "references/policy.md",
            """
            # Reconciliation policy

            Preserve the warehouse identifier and produce one canonical row per SKU.
            """,
        )
        self._write(
            main,
            "scripts/check.py",
            """
            import json
            import sys

            print(json.dumps({"checked": sys.argv[1:]}))
            """,
        )
        self._write(
            main,
            "orchestration/workflow.yaml",
            """
            orchestrator_id: warehouse-reconcile
            version: "1.0"
            routing_rules:
              nightly_reconcile:
                patterns: ["reconcile.*warehouse"]
                workers: [fetch, inspect]
                sequential_workers: [transform]
                spawn_mode: parallel
                default: true
                requires_full_output: true
                required_files: [references/policy.md]
            output_contract:
              declared_modular_files:
                - raw.json
                - normalized.csv
              declared_final_artifact: "{RUN}_bundle.txt"
              declared_file_count: 3
              merge_mandatory: true
              merge_input_order: [raw.json, normalized.csv]
              merge_separator: "\\n--CUT--\\n"
              artifact_formats:
                raw.json: json
                normalized.csv: csv
                "{RUN}_bundle.txt": text
              artifact_set_policy:
                mode: exact
                artifacts:
                  - raw.json
                  - normalized.csv
                  - "{RUN}_bundle.txt"
            """,
        )
        self._write(
            main,
            "orchestration/workers/fetch.yaml",
            """
            worker_id: fetch
            name: Inventory Fetcher
            version: "1.0"
            depends_on: []
            skills: [inventory-api]
            """,
        )
        self._write(
            main,
            "orchestration/workers/inspect.yaml",
            """
            worker_id: inspect
            name: Policy Inspector
            version: "1.0"
            depends_on: []
            local_resources: [references/policy.md]
            """,
        )
        self._write(
            main,
            "orchestration/workers/transform.yaml",
            """
            worker_id: transform
            name: Inventory Transformer
            version: "1.0"
            depends_on: [fetch, inspect]
            skills: [inventory-api]
            tools: ["Bash(python scripts/check.py:*)"]
            local_resources: [scripts/check.py]
            """,
        )

        self._write(
            capability,
            "SKILL.md",
            """
            ---
            name: inventory-api
            version: "1.0"
            description: Query the bounded warehouse inventory endpoint.
            ---
            # Inventory API

            Use GET https://inventory.vendor.test/v1/items for inventory data.
            Read references/api.md before querying and use scripts/query.py for
            deterministic normalization when the workflow requests it.
            """,
        )
        self._write(
            capability,
            "references/api.md",
            """
            # API contract

            GET https://inventory.vendor.test/v1/items
            Read references/schema.json for the response schema.
            """,
        )
        self._write(
            capability,
            "references/schema.json",
            '{"type":"object","required":["warehouse","count"]}\n',
        )
        self._write(
            capability,
            "references/unselected.md",
            "GET https://hidden.vendor.test/admin\n",
        )
        self._write(
            capability,
            "scripts/query.py",
            """
            import json
            import sys

            print(json.dumps({"normalized": sys.argv[1:]}))
            """,
        )

    @staticmethod
    def _completed_worker(
        worker_id: str,
        worker_file: str,
        stage: str,
        required_result_paths: list[str],
        *,
        tool_audit_extra: dict | None = None,
    ) -> tuple[dict, dict]:
        task = {
            "goal": f"complete {worker_id}",
            "skill_name": MAIN_SKILL,
            "step_type": "worker",
            "step_id": worker_id,
            "worker_id": worker_id,
            "worker_file": worker_file,
            "workflow_stage": stage,
            "required_result_paths": required_result_paths,
        }
        tool_audit = {
            "inspected_skill_files": [worker_file],
            "read_result_paths": required_result_paths,
        }
        if tool_audit_extra:
            tool_audit.update(tool_audit_extra)
        result = {
            "results": [{
                "status": "completed",
                "skill_name": MAIN_SKILL,
                "step_type": "worker",
                "step_id": worker_id,
                "worker_id": worker_id,
                "worker_file": worker_file,
                "workflow_stage": stage,
                "result_path": f"results/{worker_id}.txt",
                "result_chars": 500,
                "required_result_paths": required_result_paths,
                "tool_audit": tool_audit,
            }],
        }
        return task, result

    async def test_nonclinical_skill_compiles_executes_and_verifies_one_closed_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_base = root / "skills"
            sandbox_root = root / "sandboxes"
            user_id = "warehouse-user"
            session_id = "a" * 32
            self._install_fixture(skills_base, user_id, session_id)

            with (
                patch.object(scanner, "USER_SKILLS_BASE", skills_base),
                patch.object(skill_manager, "USER_SKILLS_BASE", skills_base),
                patch.object(skill_script, "USER_SKILLS_BASE", skills_base),
                patch.object(path_security, "SANDBOX_ROOT", sandbox_root),
                patch.object(workspace_context, "WORKSPACE_ROOT", sandbox_root),
            ):
                records = scanner.find_all_skills(user_id, session_id)
                session_records = {
                    item["name"]: item
                    for item in records
                    if item.get("scope") == "session"
                    and item.get("name") in {MAIN_SKILL, CAPABILITY_SKILL}
                }
                self.assertEqual(
                    {MAIN_SKILL, CAPABILITY_SKILL}, set(session_records)
                )
                self.assertTrue(all(
                    item["name"] == Path(item["skill_dir"]).name
                    for item in session_records.values()
                ))

                loaded: dict[str, dict] = {}
                runnable_scripts: dict[str, tuple[tuple[str, str], ...]] = {}
                for name, record in session_records.items():
                    package = load_skill_content(
                        Path(record["path"]),
                        skill_dir=record["skill_dir"],
                        session_id=session_id,
                    )
                    package = dict(package)
                    package["_chatds_scope"] = "session"
                    loaded[name] = package
                    runnable_scripts[name] = scanner.skill_runnable_script_resources(
                        name, user_id, session_id
                    )

                execution = loaded[MAIN_SKILL]["execution_contract"]
                self.assertTrue(execution["diagnostics"]["valid"])
                route = next(
                    item for item in execution["routes"]
                    if item["id"] == "nightly_reconcile"
                )
                self.assertEqual(
                    ["parallel", "sequential"],
                    [wave["mode"] for wave in route["waves"]],
                )
                self.assertEqual(
                    [["fetch", "inspect"], ["transform"]],
                    [wave["workers"] for wave in route["waves"]],
                )

                user_text = (
                    "Please run warehouse-reconcile to reconcile warehouse "
                    "inventory.\nRUN=nightly\n"
                )
                available_tools = [
                    "skills_list", "skill_view", "delegate_task",
                    "skill_http_get", "run_skill_script",
                    "run_declared_command", "read_file", "search_files",
                    "write_file", "patch_file", "merge_files",
                ]
                exposure = _bounded_skill_execution_exposure(
                    user_text,
                    available_tools,
                    set(session_records),
                    loaded,
                    runnable_scripts,
                    selected_skill_names=(MAIN_SKILL,),
                )
                self.assertEqual((), exposure.missing_requirements)
                self.assertTrue({
                    "skill_http_get", "run_skill_script",
                    "run_declared_command", "delegate_task", "merge_files",
                }.issubset(exposure.tools))
                self.assertIn(
                    (CAPABILITY_SKILL, "https://inventory.vendor.test/v1/items"),
                    exposure.allowed_skill_http_prefixes,
                )
                self.assertFalse(any(
                    "hidden.vendor.test" in prefix
                    for _skill, prefix in exposure.allowed_skill_http_prefixes
                ))
                capability_resources = {
                    path for skill, path in exposure.allowed_skill_resources
                    if skill == CAPABILITY_SKILL
                }
                # A capability name authorizes its main instructions only;
                # supporting files do not become an ambient child browser.
                self.assertEqual({"SKILL.md"}, capability_resources)
                self.assertTrue(any(
                    skill == CAPABILITY_SKILL and path == "scripts/query.py"
                    for skill, path, _digest in exposure.allowed_skill_scripts
                ))
                transform_command = next(
                    item for item in exposure.allowed_skill_commands
                    if item[0] == MAIN_SKILL and item[2] == "python"
                )

                root_context = ToolContext(
                    user_id=user_id,
                    session_id=session_id,
                    run_id="root-run",
                    root_run_id="root-run",
                    agent_kind="primary",
                    enabled_tools=tuple(exposure.tools),
                    skill_execution_resource_boundary=True,
                    allowed_skill_resources=exposure.allowed_skill_resources,
                    allowed_skill_scripts=exposure.allowed_skill_scripts,
                    allowed_skill_commands=exposure.allowed_skill_commands,
                    allowed_skill_http_prefixes=exposure.allowed_skill_http_prefixes,
                )
                state = HarnessRunState(
                    user_id=user_id,
                    session_id=session_id,
                    available_tools=set(exposure.tools),
                    original_user_text=user_text,
                    skill_workflow_activation="explicit_skill_request",
                )
                state.session_skill_names.update(session_records)

                main_view = json.loads(await dispatch(
                    "skill_view", {"name": MAIN_SKILL}, context=root_context
                ))
                self.assertTrue(main_view["success"])
                state.record_skill_view({"name": MAIN_SKILL}, main_view)

                inspected: list[str] = []
                while True:
                    needs_more, reason = state.needs_more_skill_workflow()
                    self.assertTrue(needs_more, reason)
                    target, error = _compiled_skill_inspection_target(state, reason)
                    self.assertFalse(error)
                    if target is None:
                        break
                    viewed = json.loads(await dispatch(
                        "skill_view", target, context=root_context
                    ))
                    self.assertTrue(viewed["success"], viewed)
                    inspected.append(target["file_path"])
                    state.record_skill_view(target, viewed)

                self.assertIn("orchestration/workflow.yaml", inspected)
                self.assertIn("references/policy.md", inspected)
                self.assertIn("parallel", reason)
                self.assertEqual(
                    "matched",
                    state.skill_execution_plans[MAIN_SKILL]["selection"],
                )
                self.assertEqual(
                    "nightly_reconcile",
                    state.skill_execution_plans[MAIN_SKILL]["route_id"],
                )

                fetch_http_grants = tuple(
                    _exact_capability_skill_http_grants(
                        [CAPABILITY_SKILL], context=root_context
                    )
                )
                self.assertEqual(
                    (
                        (
                            CAPABILITY_SKILL,
                            "https://inventory.vendor.test/v1/items",
                        ),
                    ),
                    fetch_http_grants,
                )
                fetch_http_context = ToolContext(
                    user_id=user_id,
                    session_id=session_id,
                    run_id="fetch-run",
                    root_run_id="root-run",
                    agent_kind="delegate",
                    enabled_tools=("skill_http_get",),
                    skill_execution_resource_boundary=True,
                    allowed_skill_http_prefixes=fetch_http_grants,
                )
                _FakeHttpSession.calls.clear()
                with (
                    patch.object(
                        skill_http,
                        "_public_addresses",
                        AsyncMock(return_value=(("203.0.113.10", 2),)),
                    ),
                    patch.object(
                        skill_http.aiohttp, "ClientSession", _FakeHttpSession
                    ),
                ):
                    http_result = json.loads(await dispatch(
                        "skill_http_get",
                        {"url": "https://inventory.vendor.test/v1/items?warehouse=W-7"},
                        context=fetch_http_context,
                    ))
                self.assertEqual("success", http_result["status"])
                self.assertEqual(CAPABILITY_SKILL, http_result["matched_skill"])
                self.assertEqual(1, len(_FakeHttpSession.calls))

                fetch_task, fetch_result = self._completed_worker(
                    "fetch",
                    "orchestration/workers/fetch.yaml",
                    "parallel",
                    [],
                    tool_audit_extra={
                        "successful_tool_calls": [{
                            "tool_name": "skill_http_get",
                            "matched_skill": CAPABILITY_SKILL,
                        }],
                    },
                )
                inspect_task, inspect_result = self._completed_worker(
                    "inspect",
                    "orchestration/workers/inspect.yaml",
                    "parallel",
                    [],
                )
                parallel_update = state.record_delegate_task(
                    {"tasks": [fetch_task, inspect_task]},
                    {"results": [
                        fetch_result["results"][0],
                        inspect_result["results"][0],
                    ]},
                )
                self.assertEqual(
                    ["fetch", "inspect"], parallel_update["completed_step_ids"]
                )
                needs_more, reason = state.needs_more_skill_workflow()
                self.assertTrue(needs_more)
                self.assertIn("transform", reason)

                plan = state.skill_execution_plans[MAIN_SKILL]
                transform_paths = _prerequisite_result_paths(
                    state,
                    MAIN_SKILL,
                    plan,
                    "worker",
                    worker_id="transform",
                    wave=plan["waves"][1],
                )
                self.assertEqual(
                    ["results/fetch.txt", "results/inspect.txt"],
                    transform_paths,
                )

                transform_grant_task = {
                    "skill_name": MAIN_SKILL,
                    "step_type": "worker",
                    "step_id": "transform",
                    "worker_id": "transform",
                }
                transform_script_grants = tuple(
                    _exact_declared_skill_script_grants(
                        skill_name=MAIN_SKILL,
                        skill_preload_paths=[
                            "orchestration/workers/transform.yaml"
                        ],
                        required_capability_skills=[CAPABILITY_SKILL],
                        user_id=user_id,
                        session_id=session_id,
                        context=root_context,
                    )
                )
                transform_command_grants = tuple(
                    _exact_declared_skill_command_grants(
                        task=transform_grant_task,
                        required_capability_skills=[CAPABILITY_SKILL],
                        context=root_context,
                    )
                )
                self.assertTrue(any(
                    skill == CAPABILITY_SKILL and path == "scripts/query.py"
                    for skill, path, _digest in transform_script_grants
                ))
                self.assertEqual((transform_command,), transform_command_grants)
                transform_context = ToolContext(
                    user_id=user_id,
                    session_id=session_id,
                    run_id="transform-run",
                    root_run_id="root-run",
                    agent_kind="delegate",
                    enabled_tools=("run_skill_script", "run_declared_command"),
                    skill_execution_resource_boundary=True,
                    allowed_skill_scripts=transform_script_grants,
                    allowed_skill_commands=transform_command_grants,
                )
                isolated_script = AsyncMock(return_value={
                    "status": "success",
                    "stdout": '{"normalized":["W-7"]}\n',
                    "stderr": "",
                    "artifacts": [],
                    "interpreter": "python",
                    "network": "disabled",
                })
                isolated_command = AsyncMock(return_value={
                    "status": "success",
                    "stdout": '{"checked":["W-7"]}\n',
                    "stderr": "",
                    "artifacts": [],
                    "shell": False,
                    "network": "disabled",
                    "command_sha256": "1" * 64,
                })
                with (
                    patch.object(
                        skill_script,
                        "execute_isolated_skill_script",
                        isolated_script,
                    ),
                    patch.object(
                        declared_command,
                        "preflight_isolated_skill_runtime",
                        return_value={"valid": True},
                    ),
                    patch.object(
                        declared_command,
                        "execute_isolated_declared_command",
                        isolated_command,
                    ),
                ):
                    script_result = json.loads(await dispatch(
                        "run_skill_script",
                        {
                            "script_path": "skills/inventory-api/scripts/query.py",
                            "args": ["W-7"],
                            "cwd": "script",
                        },
                        context=transform_context,
                    ))
                    command_result = json.loads(await dispatch(
                        "run_declared_command",
                        {
                            "skill_name": MAIN_SKILL,
                            "command_id": transform_command[1],
                            "argv": ["W-7"],
                            "cwd": "skill",
                        },
                        context=transform_context,
                    ))
                self.assertEqual("success", script_result["status"])
                self.assertEqual("success", command_result["status"])
                isolated_script.assert_awaited_once()
                isolated_command.assert_awaited_once()
                self.assertEqual(
                    "scripts/query.py",
                    isolated_script.await_args.kwargs["entrypoint"],
                )
                self.assertEqual(
                    ["scripts/check.py", "W-7"],
                    isolated_command.await_args.kwargs["argv"],
                )

                transform_task, transform_result = self._completed_worker(
                    "transform",
                    "orchestration/workers/transform.yaml",
                    "sequential",
                    transform_paths,
                    tool_audit_extra={
                        "successful_tool_calls": [
                            {"tool_name": "run_skill_script"},
                            {"tool_name": "run_declared_command"},
                        ],
                    },
                )
                transform_update = state.record_delegate_task(
                    transform_task, transform_result
                )
                self.assertEqual(
                    ["transform"], transform_update["completed_step_ids"]
                )

                artifact_plan = state.skill_artifact_plans[MAIN_SKILL]
                self.assertTrue(artifact_plan["valid"], artifact_plan)
                self.assertEqual(
                    "nightly_bundle.txt",
                    artifact_plan["merge"]["output_path"],
                )
                self.assertEqual([], artifact_plan["unresolved_placeholders"])

                needs_more, reason = state.needs_more_skill_workflow()
                self.assertTrue(needs_more)
                self.assertIn("modular", reason)
                synthesis_paths = _prerequisite_result_paths(
                    state, MAIN_SKILL, plan, "artifact_synthesis"
                )
                self.assertEqual(
                    [
                        "results/fetch.txt",
                        "results/inspect.txt",
                        "results/transform.txt",
                    ],
                    synthesis_paths,
                )

                raw_content = '{"warehouse":"W-7","items":[{"sku":"A","qty":2}]}\n'
                csv_content = "sku,qty\nA,2\n"
                receipts: list[dict] = []
                for call_id, path, content in (
                    ("write-raw", "raw.json", raw_content),
                    ("write-csv", "normalized.csv", csv_content),
                ):
                    written = await dispatch(
                        "write_file",
                        {"filepath": path, "content": content},
                        context=root_context,
                    )
                    payloads = _artifact_payloads_from_tool_result(
                        "write_file", written
                    )
                    self.assertEqual(1, len(payloads), written)
                    payload = payloads[0]
                    payload["tool_call_id"] = call_id
                    payload["sha256"] = hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest()
                    receipts.append(payload)

                synthesis_task = {
                    "goal": "write exact declared data artifacts",
                    "skill_name": MAIN_SKILL,
                    "step_type": "artifact_synthesis",
                    "step_id": "modular-package",
                    "workflow_stage": "artifact-synthesis",
                    "required_result_paths": synthesis_paths,
                    "required_output_ids": ["raw.json", "normalized.csv"],
                }
                synthesis_result = {
                    "results": [{
                        "status": "completed",
                        "skill_name": MAIN_SKILL,
                        "step_type": "artifact_synthesis",
                        "step_id": "modular-package",
                        "workflow_stage": "artifact-synthesis",
                        "result_path": "results/modular-package.txt",
                        "result_chars": 500,
                        "required_result_paths": synthesis_paths,
                        "artifact_receipts": receipts,
                        "tool_audit": {
                            "read_result_paths": synthesis_paths,
                        },
                    }],
                }
                synthesis_update = state.record_delegate_task(
                    synthesis_task, synthesis_result
                )
                self.assertEqual(
                    ["modular-package"],
                    synthesis_update["completed_step_ids"],
                )

                needs_more, reason = state.needs_more_skill_workflow()
                self.assertTrue(needs_more)
                self.assertIn("merged", reason)
                merge_args = {
                    "output_filepath": artifact_plan["merge"]["output_path"],
                    "input_files": artifact_plan["merge"]["input_paths"],
                    "separator": artifact_plan["merge"]["separator"],
                }
                merged = await dispatch(
                    "merge_files", merge_args, context=root_context
                )
                merge_payloads = _artifact_payloads_from_tool_result(
                    "merge_files", merged
                )
                self.assertEqual(1, len(merge_payloads), merged)
                state.artifacts.extend(merge_payloads)

                workspace = sandbox_root / user_id / session_id / "workspace"
                final_path = workspace / "nightly_bundle.txt"
                self.assertEqual(
                    raw_content + "\n--CUT--\n" + csv_content,
                    final_path.read_text(encoding="utf-8"),
                )
                self.assertNotIn(
                    "{RUN}", "\n".join(str(item) for item in merge_args.values())
                )
                self.assertEqual((False, ""), state.needs_more_skill_workflow())

                verifier = _deterministic_verifier_payload(state)
                self.assertIsNotNone(verifier)
                self.assertFalse(verifier["needs_more_work"], verifier)
                self.assertTrue(verifier["artifact_contract"]["valid"])
                self.assertEqual(
                    {"raw.json", "normalized.csv", "nightly_bundle.txt"},
                    {item["path"] for item in state.artifacts},
                )

    def test_persistent_process_raw_sync_and_close_results_project_artifacts(self):
        artifact = {
            "path": "generated/process-output.json",
            "size_bytes": 37,
            "sha256": "b" * 64,
        }
        for operation in ("sync", "close"):
            raw = json.dumps({
                "status": "success",
                "operation": operation,
                "artifacts": [artifact],
            })
            with self.subTest(operation=operation):
                self.assertEqual(
                    [{
                        "kind": "file",
                        "title": "process-output.json",
                        "path": "generated/process-output.json",
                        "source_tool": "run_skill_process",
                        "size_bytes": 37,
                        "sha256": "b" * 64,
                    }],
                    _artifact_payloads_from_tool_result(
                        "run_skill_process",
                        raw,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
