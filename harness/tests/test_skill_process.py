from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import agent_loop
import main as harness_main
from tools.context import ToolContext
from tools import isolated_skill_executor as isolated_executor
from tools.isolated_skill_executor import (
    IsolatedProcessLease,
    IsolatedSkillExecutorError,
    compute_skill_package_digest,
    create_process_owner_scope,
)
from tools.registry import get_metadata, get_schemas, preflight
from tools import skill_process
from tools.workspace_lock import WorkspaceMutationLockError


class _FakeSlotReservation:
    def __init__(self) -> None:
        self.terminal = False
        self.actions: list[tuple[str, str | None]] = []

    async def release(self) -> None:
        self.actions.append(("release", None))
        self.terminal = True

    async def quarantine(self, reason: str) -> None:
        self.actions.append(("quarantine", reason))
        self.terminal = True


class SkillProcessToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.skill = self.root / "skill"
        self.workspace = self.root / "workspace"
        (self.skill / "scripts").mkdir(parents=True)
        self.workspace.mkdir()
        self.skill_md = self.skill / "SKILL.md"
        self.skill_md.write_text(
            "---\nname: portable-process\n---\n"
            "Use scripts/session.cjs.\n",
            encoding="utf-8",
        )
        self.script = self.skill / "scripts" / "session.cjs"
        self.script.write_text(
            '"use strict";\n'
            'const readline = require("readline");\n'
            "readline.createInterface({input: process.stdin})"
            '.on("line", line => console.log(line));\n',
            encoding="utf-8",
        )
        self.manager = skill_process.SkillProcessManager()
        self.manager_patch = patch.object(skill_process, "_MANAGER", self.manager)
        self.manager_patch.start()
        self.resolve_patch = patch.object(
            skill_process,
            "_resolve_session_skill_script",
            side_effect=self._resolve,
        )
        self.resolve_patch.start()
        self.sandbox_patch = patch.object(
            skill_process,
            "sandbox_dir",
            return_value=self.workspace,
        )
        self.sandbox_patch.start()

    def tearDown(self) -> None:
        self.sandbox_patch.stop()
        self.resolve_patch.stop()
        self.manager_patch.stop()
        self.tempdir.cleanup()

    def _resolve(
        self,
        script_path: str,
        user_id: str,
        session_id: str,
        enabled_user_skills: list[str],
    ):
        del user_id, session_id, enabled_user_skills
        suffix = script_path.removeprefix("skills/portable-process/")
        candidate = self.skill / suffix
        return candidate.resolve(), self.skill.resolve(), "portable-process"

    def context(
        self,
        *,
        user_id: str = "user-a",
        session_id: str = "session-a",
        run_id: str = "root-a",
        root_run_id: str = "root-a",
        script: Path | None = None,
    ) -> ToolContext:
        selected = script or self.script
        script_relative = selected.relative_to(self.skill).as_posix()
        script_digest = hashlib.sha256(selected.read_bytes()).hexdigest()
        root_digest = hashlib.sha256(self.skill_md.read_bytes()).hexdigest()
        package_digest = compute_skill_package_digest(self.skill)
        return ToolContext(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            root_run_id=root_run_id,
            enabled_user_skills=("portable-process",),
            skill_execution_resource_boundary=True,
            allowed_skill_scripts=((
                "portable-process",
                script_relative,
                script_digest,
            ),),
            allowed_skill_script_authorities=((
                "portable-process",
                root_digest,
                "SKILL.md",
                root_digest,
                script_relative,
                script_digest,
            ),),
            allowed_skill_package_digests=((
                "portable-process",
                package_digest,
            ),),
            tool_operation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    def fake_lease(
        self,
        *,
        context: ToolContext,
        script: Path | None = None,
        invocation_mode: str = "cli",
        class_name: str | None = None,
        factory_name: str | None = None,
        socket_path: str = "/executor.sock",
    ) -> IsolatedProcessLease:
        selected = script or self.script
        return IsolatedProcessLease(
            handle="pl2_" + "a" * 32 + "_" + "b" * 32,
            skill_sha256=compute_skill_package_digest(self.skill),
            script_sha256=hashlib.sha256(selected.read_bytes()).hexdigest(),
            entrypoint=selected.relative_to(self.skill).as_posix(),
            invocation_mode=invocation_mode,
            class_name=class_name,
            factory_name=factory_name,
            _owner_scope=create_process_owner_scope(
                user_id=context.user_id,
                session_id=context.session_id,
                root_run_id=str(context.root_run_id),
            ),
            _workspace=self.workspace,
            _socket_path=socket_path,
            _baseline={},
        )

    async def start_process(
        self,
        *,
        context: ToolContext | None = None,
        script_path: str = "skills/portable-process/scripts/session.cjs",
        runtime_profile: str = "base-v1",
        invocation_mode: str = "cli",
        class_name: str | None = None,
        factory_name: str | None = None,
        args: list[str] | None = None,
        constructor_args: list | None = None,
        constructor_kwargs: dict | None = None,
    ) -> tuple[dict, AsyncMock, AsyncMock]:
        context = context or self.context()
        selected_script = self.skill / script_path.removeprefix(
            "skills/portable-process/"
        )
        lease = self.fake_lease(
            context=context,
            script=selected_script,
            invocation_mode=invocation_mode,
            class_name=class_name,
            factory_name=factory_name,
        )
        opened = AsyncMock(return_value=(
            lease,
            {
                "status": "success",
                "state": "open",
                "runtime_profile": "session-sandbox-v1",
                "network_policy": {
                    "direct": "disabled",
                    "egress": "none",
                },
            },
        ))
        started = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "session-sandbox-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "none",
            },
            "invocation_mode": invocation_mode,
            **({"class_name": class_name} if class_name else {}),
            **({"factory_name": factory_name} if factory_name else {}),
        })
        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                opened,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                started,
            ),
        ):
            raw = await skill_process.run_skill_process(
                operation="start",
                script_path=script_path,
                class_name=class_name,
                factory_name=factory_name,
                args=args,
                constructor_args=constructor_args,
                constructor_kwargs=constructor_kwargs,
                context=context,
            )
        return json.loads(raw), opened, started

    async def test_required_cwd_rejects_default_workspace_start(self) -> None:
        script = self.skill / "scripts" / "relative.sh"
        helper = self.skill / "scripts" / "helper.sh"
        script.write_text(
            "bash scripts/helper.sh\n",
            encoding="utf-8",
        )
        helper.write_text("printf ok\n", encoding="utf-8")
        context = self.context(script=script)

        raw = await skill_process.run_skill_process(
            operation="start",
            script_path=(
                "skills/portable-process/scripts/relative.sh"
            ),
            context=context,
        )

        result = json.loads(raw)
        self.assertEqual("error", result["status"])
        self.assertEqual(
            "skill_runtime_cwd_mismatch",
            result["error_code"],
        )

    async def test_start_binds_actual_argv_url_before_open(self) -> None:
        (self.skill / "chatds-runtime.json").write_text(
            json.dumps({
                "schema_version": 2,
                "entrypoints": {
                    "scripts/session.cjs": {
                        "runtime_profile": "base-v1",
                        "egress_only": True,
                        "user_url_egress": [{
                            "source": "argv",
                            "selector": "--url",
                            "methods": ["GET", "HEAD"],
                            "scope": "origin",
                        }],
                    },
                },
            }),
            encoding="utf-8",
        )
        context = replace(
            self.context(),
            user_url_authorization_urls=(
                "https://portal.example.test:443/news/today",
            ),
        )

        result, opened, _started = await self.start_process(
            context=context,
            args=[
                "--url",
                "https://portal.example.test/news/today",
            ],
        )

        self.assertEqual("success", result["status"])
        self.assertEqual(
            ({
                "url_prefix": "https://portal.example.test:443/",
                "methods": ["GET", "HEAD"],
            },),
            opened.await_args.kwargs["egress_rules"],
        )

    async def test_start_never_pregrants_later_stdin_json_url(self) -> None:
        (self.skill / "chatds-runtime.json").write_text(
            json.dumps({
                "schema_version": 2,
                "entrypoints": {
                    "scripts/session.cjs": {
                        "runtime_profile": "base-v1",
                        "egress_only": True,
                        "user_url_egress": [{
                            "source": "stdin_json",
                            "selector": "url",
                            "command": "goto",
                            "methods": ["GET"],
                            "scope": "origin",
                        }],
                    },
                },
            }),
            encoding="utf-8",
        )
        context = replace(
            self.context(),
            user_url_authorization_urls=(
                "https://portal.example.test:443/news/today",
            ),
        )

        result, opened, _started = await self.start_process(
            context=context,
        )

        self.assertEqual("success", result["status"])
        self.assertNotIn("egress_rules", opened.await_args.kwargs)

    async def test_structured_start_requires_complete_constructor_or_factory(
        self,
    ) -> None:
        python_script = self.skill / "scripts" / "structured.py"
        python_script.write_text(
            "class Client:\n"
            "    def __init__(self, url, required):\n"
            "        self.url = url\n"
            "        self.required = required\n\n"
            "class AsyncClient:\n"
            "    async def __init__(self, url, required):\n"
            "        self.url = url\n"
            "        self.required = required\n\n"
            "def build(url, required):\n"
            "    return Client(url, required)\n",
            encoding="utf-8",
        )
        (self.skill / "chatds-runtime.json").write_text(
            json.dumps({
                "schema_version": 2,
                "entrypoints": {
                    "scripts/structured.py": {
                        "runtime_profile": "base-v1",
                        "egress_only": True,
                        "user_url_egress": [
                            {
                                "source": "python",
                                "selector": "url",
                                "callable": "Client",
                                "methods": ["GET"],
                                "scope": "url",
                            },
                            {
                                "source": "python",
                                "selector": "url",
                                "callable": "build",
                                "methods": ["GET"],
                                "scope": "url",
                            },
                            {
                                "source": "python",
                                "selector": "url",
                                "callable": "AsyncClient",
                                "methods": ["GET"],
                                "scope": "url",
                            },
                        ],
                    },
                },
            }),
            encoding="utf-8",
        )
        authorized = "https://api.example.test:443/v1/items"
        context = replace(
            self.context(script=python_script),
            user_url_authorization_urls=(authorized,),
        )
        path = "skills/portable-process/scripts/structured.py"

        for mode, target in (
            ("instance", {"class_name": "Client"}),
            ("factory", {"factory_name": "build"}),
        ):
            with self.subTest(mode=mode, validity="missing_required"):
                result, opened, _started = await self.start_process(
                    context=context,
                    script_path=path,
                    invocation_mode=mode,
                    constructor_args=[
                        "https://api.example.test/v1/items",
                    ],
                    **target,
                )
                self.assertEqual("success", result["status"])
                self.assertNotIn(
                    "egress_rules",
                    opened.await_args.kwargs,
                )

            with self.subTest(mode=mode, validity="complete"):
                result, opened, _started = await self.start_process(
                    context=context,
                    script_path=path,
                    invocation_mode=mode,
                    constructor_args=[
                        "https://api.example.test/v1/items",
                        True,
                    ],
                    **target,
                )
                self.assertEqual("success", result["status"])
                self.assertEqual(
                    ({
                        "url_prefix": authorized,
                        "methods": ["GET"],
                    },),
                    opened.await_args.kwargs["egress_rules"],
                )

        result, opened, _started = await self.start_process(
            context=context,
            script_path=path,
            invocation_mode="instance",
            class_name="AsyncClient",
            constructor_args=[
                "https://api.example.test/v1/items",
                True,
            ],
        )
        self.assertEqual("success", result["status"])
        self.assertNotIn("egress_rules", opened.await_args.kwargs)

    def test_schema_and_metadata_do_not_expose_runtime_authority(self) -> None:
        schemas = get_schemas(["run_skill_process"])
        self.assertEqual("run_skill_process", schemas[0]["function"]["name"])
        properties = schemas[0]["function"]["parameters"]["properties"]
        self.assertEqual(
            skill_process.MAX_PROCESS_LEASE_TTL_SECONDS,
            skill_process.DEFAULT_IDLE_TTL_SECONDS,
        )
        self.assertEqual(
            skill_process.MAX_PROCESS_RUNTIME_SECONDS,
            skill_process.DEFAULT_MAX_RUNTIME_SECONDS,
        )
        for hidden in (
            "user_id",
            "session_id",
            "root_run_id",
            "owner_scope",
            "lease_handle",
            "socket_path",
            "runtime_profile",
            "token",
            "op_id",
            "operation_id",
            "tool_operation_id",
            "skill_sha256",
            "script_sha256",
        ):
            self.assertNotIn(hidden, properties)
        metadata = get_metadata("run_skill_process") or {}
        self.assertFalse(metadata["read_only"])
        self.assertTrue(metadata["destructive"])
        self.assertFalse(metadata["parallel_safe"])
        self.assertTrue(metadata["mutates_workspace"])
        self.assertTrue(metadata["external_interaction"])
        self.assertFalse(metadata["salvage_safe"])
        self.assertTrue(metadata["allow_in_child"])
        self.assertFalse(metadata["allow_in_parallel_child"])

        invalid = preflight(
            "run_skill_process",
            {
                "operation": "sync",
                "process_id": "sp_" + "a" * 32,
                "script_path": (
                    "skills/portable-process/scripts/session.cjs"
                ),
            },
            context=self.context(),
        )
        self.assertFalse(invalid.ok)
        self.assertEqual(
            "invalid_process_operation_fields",
            (invalid.error_payload or {}).get("reason"),
        )

    async def test_start_binds_stable_suboperation_ids_and_hides_lease(self) -> None:
        result, opened, started = await self.start_process()
        self.assertEqual("success", result["status"])
        self.assertNotIn("process_evidence_receipt", result)
        self.assertRegex(result["process_id"], r"^sp_")
        self.assertNotIn("lease_handle", result)
        self.assertNotIn("scope_digest", result)
        self.assertNotIn("skill_sha256", result)
        self.assertNotIn("script_sha256", result)
        open_op_id = opened.await_args.kwargs["op_id"]
        start_op_id = started.await_args.kwargs["op_id"]
        uuid.UUID(open_op_id)
        uuid.UUID(start_op_id)
        self.assertNotEqual(open_op_id, start_op_id)
        self.assertEqual(
            open_op_id,
            skill_process._operation_uuid(self.context(), "open"),
        )

    async def test_cli_receipt_requires_terminal_eof_and_classifies_exit(self) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(
            context=context,
            args=["--query", "portable"],
        )
        process_id = started["process_id"]
        pending_response = {
            "status": "success",
            "state": "running",
            "stdout_bytes": b"working\n",
            "stderr_bytes": b"",
            "stdout_start_offset": 0,
            "stdout_next_offset": 8,
            "stdout_end_offset": 8,
            "stderr_start_offset": 0,
            "stderr_next_offset": 0,
            "stderr_end_offset": 0,
            "stdout_data_loss": False,
            "stderr_data_loss": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout_eof": False,
            "stderr_eof": False,
            "returncode": None,
        }
        terminal_response = {
            **pending_response,
            "state": "exited",
            "stdout_bytes": b"",
            "stdout_start_offset": 8,
            "stdout_next_offset": 8,
            "stdout_eof": True,
            "stderr_eof": True,
            "returncode": 7,
        }
        with patch.object(
            skill_process,
            "read_isolated_process_output",
            AsyncMock(side_effect=[pending_response, terminal_response]),
        ):
            pending = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                ),
            ))
            failed = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                stdout_offset=8,
                context=replace(
                    context,
                    tool_operation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                ),
            ))
        self.assertNotIn("process_evidence_receipt", pending)
        receipt = failed["process_evidence_receipt"]
        self.assertEqual("cli_exit", receipt["completion_kind"])
        self.assertEqual("error", receipt["outcome"])
        self.assertEqual(7, receipt["returncode"])
        self.assertEqual(process_id, receipt["process_id"])

    async def test_structured_receipt_waits_for_exact_complete_call_result(
        self,
    ) -> None:
        python_script = self.skill / "scripts" / "operator.py"
        python_script.write_text(
            "class BrowserSession:\n"
            "    def snapshot(self, value=None):\n"
            "        return value\n",
            encoding="utf-8",
        )
        context = self.context(script=python_script)
        started, _opened, _start = await self.start_process(
            context=context,
            script_path="skills/portable-process/scripts/operator.py",
            invocation_mode="instance",
            class_name="BrowserSession",
        )
        process_id = started["process_id"]
        call_context = replace(
            context,
            tool_operation_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        )
        call_id = skill_process._operation_uuid(call_context, "call")
        call_response = {
            "status": "success",
            "state": "running",
            "method_name": "snapshot",
            "call_id": call_id,
            "partial": False,
        }
        envelope = json.dumps({
            "event": "call_result",
            "call_id": call_id,
            "method": "snapshot",
            "status": "success",
            "result": {"status": "success", "value": 1},
        }, separators=(",", ":")).encode("utf-8") + b"\n"
        split = len(envelope) // 2

        async def read_response(offset: int, chunk: bytes) -> dict:
            return {
                "status": "success",
                "state": "running",
                "stdout_bytes": chunk,
                "stderr_bytes": b"",
                "stdout_start_offset": offset,
                "stdout_next_offset": offset + len(chunk),
                "stdout_end_offset": len(envelope),
                "stderr_start_offset": 0,
                "stderr_next_offset": 0,
                "stderr_end_offset": 0,
                "stdout_data_loss": False,
                "stderr_data_loss": False,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "stdout_eof": False,
                "stderr_eof": False,
                "returncode": None,
            }

        with (
            patch.object(
                skill_process,
                "call_isolated_process_instance",
                AsyncMock(return_value=call_response),
            ),
            patch.object(
                skill_process,
                "read_isolated_process_output",
                AsyncMock(side_effect=[
                    await read_response(0, envelope[:split]),
                    await read_response(split, envelope[split:]),
                ]),
            ),
            patch.object(
                skill_process,
                "sync_isolated_process_artifacts",
                AsyncMock(return_value={
                    "status": "success",
                    "state": "running",
                    "workspace_applied": True,
                    "sync_pending": False,
                    "sync_acknowledged": True,
                    "acknowledged_operation": "sync",
                    "artifacts": [{
                        "path": "pending.json",
                        "size_bytes": 3,
                        "sha256": "a" * 64,
                    }],
                }),
            ),
        ):
            enqueued = json.loads(await skill_process.run_skill_process(
                operation="call",
                process_id=process_id,
                method_name="snapshot",
                method_args=[{"full": True}],
                context=call_context,
            ))
            pending_sync = json.loads(await skill_process.run_skill_process(
                operation="sync",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id=(
                        "abababab-abab-4bab-8bab-abababababab"
                    ),
                ),
            ))
            first = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                ),
            ))
            completed = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                stdout_offset=split,
                context=replace(
                    context,
                    tool_operation_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
                ),
            ))
        self.assertNotIn("process_evidence_receipt", enqueued)
        self.assertNotIn("process_evidence_receipt", pending_sync)
        self.assertNotIn("process_evidence_receipt", first)
        receipt = completed["process_evidence_receipt"]
        self.assertEqual("structured_call", receipt["completion_kind"])
        self.assertEqual("success", receipt["outcome"])
        self.assertEqual(call_id, receipt["call_id"])
        self.assertEqual(process_id, receipt["process_id"])
        with patch.object(
            skill_process,
            "sync_isolated_process_artifacts",
            AsyncMock(return_value={
                "status": "success",
                "state": "running",
                "workspace_applied": True,
                "sync_pending": False,
                "sync_acknowledged": True,
                "acknowledged_operation": "sync",
                "artifacts": [{
                    "path": "result.json",
                    "size_bytes": 3,
                    "sha256": "d" * 64,
                }],
            }),
        ):
            synced = json.loads(await skill_process.run_skill_process(
                operation="sync",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id=(
                        "25252525-2525-4525-8525-252525252525"
                    ),
                ),
            ))
        artifact_receipt = synced["process_evidence_receipt"]
        self.assertEqual(
            "artifact_sync",
            artifact_receipt["completion_kind"],
        )
        self.assertEqual(call_id, artifact_receipt["call_id"])
        self.assertEqual(
            receipt["receipt_id"],
            artifact_receipt["receipt_id"],
        )
        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            AsyncMock(return_value={
                "status": "success",
                "state": "closed",
                "workspace_applied": True,
                "sync_pending": False,
                "sync_acknowledged": True,
                "acknowledged_operation": "close",
                "returncode": 1,
                "artifacts": [{
                    "path": "late.json",
                    "size_bytes": 3,
                    "sha256": "c" * 64,
                }],
            }),
        ):
            closed = json.loads(await skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id=(
                        "24242424-2424-4424-8424-242424242424"
                    ),
                ),
            ))
        self.assertNotIn("process_evidence_receipt", closed)

    async def test_structured_typed_failure_is_terminal_error_evidence(self) -> None:
        python_script = self.skill / "scripts" / "operator.py"
        python_script.write_text(
            "class BrowserSession:\n"
            "    def query(self, value=None):\n"
            "        return value\n",
            encoding="utf-8",
        )
        context = self.context(script=python_script)
        started, _opened, _start = await self.start_process(
            context=context,
            script_path="skills/portable-process/scripts/operator.py",
            invocation_mode="instance",
            class_name="BrowserSession",
        )
        process_id = started["process_id"]
        call_context = replace(
            context,
            tool_operation_id="11111111-1111-4111-8111-111111111111",
        )
        call_id = skill_process._operation_uuid(call_context, "call")
        line = json.dumps({
            "event": "call_result",
            "call_id": call_id,
            "method": "query",
            "status": "success",
            "result": {"status": "error", "error": "upstream unavailable"},
        }, separators=(",", ":")).encode("utf-8") + b"\n"
        read_response = {
            "status": "success",
            "state": "running",
            "stdout_bytes": line,
            "stderr_bytes": b"",
            "stdout_start_offset": 0,
            "stdout_next_offset": len(line),
            "stdout_end_offset": len(line),
            "stderr_start_offset": 0,
            "stderr_next_offset": 0,
            "stderr_end_offset": 0,
            "stdout_data_loss": False,
            "stderr_data_loss": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout_eof": False,
            "stderr_eof": False,
            "returncode": None,
        }
        with (
            patch.object(
                skill_process,
                "call_isolated_process_instance",
                AsyncMock(return_value={
                    "status": "success",
                    "state": "running",
                    "method_name": "query",
                    "call_id": call_id,
                    "partial": False,
                }),
            ),
            patch.object(
                skill_process,
                "read_isolated_process_output",
                AsyncMock(return_value=read_response),
            ),
        ):
            await skill_process.run_skill_process(
                operation="call",
                process_id=process_id,
                method_name="query",
                method_args=["term"],
                context=call_context,
            )
            completed = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id="22222222-2222-4222-8222-222222222222",
                ),
            ))
        receipt = completed["process_evidence_receipt"]
        self.assertEqual("error", receipt["outcome"])
        self.assertTrue(
            receipt["callable_result_receipt"]["typed_failure"]
        )
        with patch.object(
            skill_process,
            "sync_isolated_process_artifacts",
            AsyncMock(return_value={
                "status": "success",
                "state": "running",
                "workspace_applied": True,
                "sync_pending": False,
                "sync_acknowledged": True,
                "acknowledged_operation": "sync",
                "artifacts": [{
                    "path": "failed.json",
                    "size_bytes": 3,
                    "sha256": "b" * 64,
                }],
            }),
        ):
            synced = json.loads(await skill_process.run_skill_process(
                operation="sync",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id=(
                        "23232323-2323-4323-8323-232323232323"
                    ),
                ),
            ))
        self.assertEqual("failed.json", synced["artifacts"][0]["path"])
        self.assertNotIn("process_evidence_receipt", synced)

    async def test_cross_session_and_cross_root_process_ids_are_rejected(self) -> None:
        result, _opened, _started = await self.start_process()
        process_id = result["process_id"]
        read = AsyncMock()
        with patch.object(
            skill_process,
            "read_isolated_process_output",
            read,
        ):
            cross_session = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=self.context(session_id="session-b"),
            ))
            cross_root = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=self.context(run_id="root-b", root_run_id="root-b"),
            ))
        self.assertEqual("process_scope_mismatch", cross_session["error_code"])
        self.assertEqual("process_scope_mismatch", cross_root["error_code"])
        read.assert_not_awaited()

    async def test_browser_import_uses_the_single_session_sandbox(self) -> None:
        self.script.write_text(
            'const { chromium } = require("playwright");\n',
            encoding="utf-8",
        )
        context = self.context()
        lease = self.fake_lease(
            context=context,
            socket_path="/base.sock",
        )
        opened = AsyncMock(return_value=(lease, {
            "status": "success",
            "state": "open",
            "runtime_profile": "session-sandbox-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "none",
            },
        }))
        started = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "session-sandbox-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "none",
            },
        })
        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": "/base.sock",
                    "SKILL_BROWSER_EXECUTOR_SOCKET": "",
                },
            ),
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                opened,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                started,
            ),
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path="skills/portable-process/scripts/session.cjs",
                context=context,
            ))
        self.assertEqual("success", result["status"])
        self.assertEqual(
            "/base.sock",
            opened.await_args.kwargs["socket_path"],
        )

        opened.reset_mock()
        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": "/base.sock",
                    "SKILL_BROWSER_EXECUTOR_SOCKET": "/browser.sock",
                },
            ),
            patch.object(skill_process, "open_isolated_process_lease", opened),
            patch.object(skill_process, "start_isolated_process_lease", started),
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path="skills/portable-process/scripts/session.cjs",
                context=context,
            ))
        self.assertEqual(
            "session_sandbox_topology_mismatch",
            result["error_code"],
        )
        opened.assert_not_awaited()

    async def test_indirect_helper_and_shell_wrapper_select_browser_runtime(
        self,
    ) -> None:
        helper = self.skill / "scripts" / "browser_helper.cjs"
        helper.write_text(
            'const { chromium } = require("playwright");\n'
            "module.exports = chromium;\n",
            encoding="utf-8",
        )
        self.script.write_text(
            'require("./browser_helper.cjs");\n',
            encoding="utf-8",
        )
        context = self.context()
        with patch.dict(
            os.environ,
            {
                "EXECUTOR_SOCKET": "/session-sandbox.sock",
                "SKILL_BROWSER_EXECUTOR_SOCKET": (
                    "/session-sandbox.sock"
                ),
            },
        ):
            result, opened, _started = await self.start_process(
                context=context,
                runtime_profile="browser-automation-v1",
            )
        self.assertEqual("success", result["status"])
        self.assertEqual(
            "/session-sandbox.sock",
            opened.await_args.kwargs["socket_path"],
        )

        shell = self.skill / "scripts" / "launch.sh"
        shell.write_text(
            "#!/bin/sh\nnode scripts/browser_helper.cjs\n",
            encoding="utf-8",
        )
        self.assertEqual(
            "browser-automation-v1",
            skill_process._runtime_profile_for_script(shell, self.skill),
        )

    def test_multilingual_skill_prose_does_not_select_browser_runtime(self) -> None:
        self.skill_md.write_text(
            "---\nname: portable-process\n---\n"
            "使用浏览器检查结果。افحص الصفحة في المتصفح. "
            "ブラウザーで結果を確認します。\n",
            encoding="utf-8",
        )
        self.script.write_text(
            '"use strict";\nconsole.log("普通の処理");\n',
            encoding="utf-8",
        )
        helper = self.skill / "scripts" / "helper.py"
        helper.write_text(
            "# Selenium と Playwright は説明文だけです。\n"
            "print('普通处理')\n",
            encoding="utf-8",
        )
        self.assertEqual(
            "base-v1",
            skill_process._runtime_profile_for_script(
                self.script,
                self.skill,
            ),
        )

    def test_unrelated_browser_route_does_not_change_entrypoint_profile(
        self,
    ) -> None:
        self.script.write_text(
            '"use strict";\nconsole.log("base route");\n',
            encoding="utf-8",
        )
        unrelated = self.skill / "scripts" / "unrelated_browser.cjs"
        unrelated.write_text(
            'const { chromium } = require("playwright");\n'
            "module.exports = chromium;\n",
            encoding="utf-8",
        )
        self.assertEqual(
            "base-v1",
            skill_process._runtime_profile_for_script(
                self.script,
                self.skill,
            ),
        )

        self.script.write_text(
            'require("./unrelated_browser.cjs");\n',
            encoding="utf-8",
        )
        self.assertEqual(
            "browser-automation-v1",
            skill_process._runtime_profile_for_script(
                self.script,
                self.skill,
            ),
        )

    def test_unattested_browser_dependency_fails_before_routing(self) -> None:
        self.script.write_text(
            'const puppeteer = require("puppeteer");\n'
            "console.log(puppeteer);\n",
            encoding="utf-8",
        )
        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            skill_process._runtime_profile_for_script(
                self.script,
                self.skill,
            )
        self.assertEqual(
            "browser_runtime_dependency_unsupported",
            raised.exception.code,
        )

    async def test_digest_mutation_is_rejected_before_dispatch(self) -> None:
        context = self.context()
        self.script.write_text("console.log('mutated');\n", encoding="utf-8")
        opened = AsyncMock()
        with patch.object(
            skill_process,
            "open_isolated_process_lease",
            opened,
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path="skills/portable-process/scripts/session.cjs",
                context=context,
            ))
        self.assertEqual("skill_script_authority_mismatch", result["error_code"])
        opened.assert_not_awaited()

    async def test_unreferenced_helper_mutation_and_new_file_fail_package_grant(
        self,
    ) -> None:
        helper = self.skill / "scripts" / "helper.js"
        helper.write_text("exports.value = 1;\n", encoding="utf-8")
        context = self.context()
        opened = AsyncMock()
        helper.write_text("exports.value = 2;\n", encoding="utf-8")
        with patch.object(
            skill_process,
            "open_isolated_process_lease",
            opened,
        ):
            mutated = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path="skills/portable-process/scripts/session.cjs",
                context=context,
            ))
        self.assertEqual(
            "skill_package_authority_mismatch",
            mutated["error_code"],
        )
        opened.assert_not_awaited()

        helper.write_text("exports.value = 1;\n", encoding="utf-8")
        context = self.context()
        (self.skill / "scripts" / "late.js").write_text(
            "exports.late = true;\n",
            encoding="utf-8",
        )
        with patch.object(
            skill_process,
            "open_isolated_process_lease",
            opened,
        ):
            added = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path="skills/portable-process/scripts/session.cjs",
                context=context,
            ))
        self.assertEqual(
            "skill_package_authority_mismatch",
            added["error_code"],
        )
        opened.assert_not_awaited()

    async def test_package_addition_during_open_cannot_change_sent_snapshot(self) -> None:
        context = self.context()
        lease = self.fake_lease(context=context)

        async def add_file_during_open(**_kwargs):
            (self.skill / "scripts" / "late.js").write_text(
                "exports.late = true;\n",
                encoding="utf-8",
            )
            return lease, {
                "status": "success",
                "runtime_profile": "session-sandbox-v1",
            }

        close = AsyncMock()
        started = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "session-sandbox-v1",
        })
        opened = AsyncMock(side_effect=add_file_during_open)
        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                opened,
            ),
            patch.object(
                skill_process,
                "close_isolated_process_lease",
                close,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                started,
            ),
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path="skills/portable-process/scripts/session.cjs",
                context=context,
            ))
        self.assertEqual("success", result["status"])
        sent_snapshot = opened.await_args.kwargs["skill_snapshot"]
        self.assertNotIn("scripts/late.js", sent_snapshot.paths)
        self.assertEqual(
            context.allowed_skill_package_digests[0][1],
            sent_snapshot.sha256,
        )
        started.assert_awaited_once()
        close.assert_not_awaited()

    async def test_mutation_during_open_cannot_change_sent_snapshot(self) -> None:
        context = self.context()
        lease = self.fake_lease(context=context)
        original_script = self.script.read_bytes()

        async def mutate_during_open(**_kwargs):
            self.script.write_text("console.log('changed');\n", encoding="utf-8")
            return lease, {
                "status": "success",
                "runtime_profile": "session-sandbox-v1",
            }

        close = AsyncMock()
        started = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "session-sandbox-v1",
        })
        opened = AsyncMock(side_effect=mutate_during_open)
        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                opened,
            ),
            patch.object(
                skill_process,
                "close_isolated_process_lease",
                close,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                started,
            ),
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path="skills/portable-process/scripts/session.cjs",
                context=context,
            ))
        self.assertEqual("success", result["status"])
        sent_snapshot = opened.await_args.kwargs["skill_snapshot"]
        self.assertEqual(
            original_script,
            sent_snapshot.read_bytes("scripts/session.cjs"),
        )
        close.assert_not_awaited()
        started.assert_awaited_once()

    async def test_commonjs_write_incremental_read_and_sync_flow(self) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        write = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "bytes_written": 10,
            "requested_bytes": 10,
            "partial": False,
            "stdin_total_bytes": 10,
        })
        stdin_close = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "stdin_closed": True,
            "already_closed": False,
        })
        read = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "stdout_bytes": b'{"ok":1}\n',
            "stderr_bytes": b"",
            "stdout_start_offset": 4,
            "stdout_next_offset": 13,
            "stdout_end_offset": 13,
            "stderr_start_offset": 0,
            "stderr_next_offset": 0,
            "stderr_end_offset": 0,
            "stdout_data_loss": False,
            "stderr_data_loss": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout_eof": False,
            "stderr_eof": False,
        })
        sync = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "workspace_applied": True,
            "sync_pending": False,
            "sync_acknowledged": True,
            "acknowledged_operation": "sync",
            "artifacts": [{
                "kind": "file",
                "path": "shot.png",
                "change": "created",
                "size_bytes": 12,
                "sha256": "d" * 64,
            }],
        })
        with (
            patch.object(
                skill_process,
                "write_isolated_process_stdin",
                write,
            ),
            patch.object(
                skill_process,
                "close_isolated_process_stdin",
                stdin_close,
            ),
            patch.object(
                skill_process,
                "read_isolated_process_output",
                read,
            ),
            patch.object(
                skill_process,
                "sync_isolated_process_artifacts",
                sync,
            ),
        ):
            written = json.loads(await skill_process.run_skill_process(
                operation="write",
                process_id=process_id,
                data='{"go":1}\n',
                context=context,
            ))
            eof = json.loads(await skill_process.run_skill_process(
                operation="stdin_close",
                process_id=process_id,
                context=context,
            ))
            observed = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                stdout_offset=4,
                max_bytes=4096,
                wait_ms=250,
                context=context,
            ))
            synced = json.loads(await skill_process.run_skill_process(
                operation="sync",
                process_id=process_id,
                context=context,
            ))
        self.assertEqual(10, written["bytes_written"])
        write.assert_awaited_once()
        self.assertEqual('{"go":1}\n', write.await_args.args[1])
        self.assertTrue(eof["stdin_closed"])
        self.assertFalse(eof["already_closed"])
        self.assertEqual(
            skill_process._operation_uuid(context, "stdin-close"),
            stdin_close.await_args.kwargs["op_id"],
        )
        self.assertEqual('{"ok":1}\n', observed["stdout_text"])
        self.assertEqual("utf-8", observed["stdout_encoding"])
        self.assertNotIn("stdout_base64", observed)
        self.assertEqual(13, observed["stdout_next_offset"])
        self.assertEqual("shot.png", synced["artifacts"][0]["path"])
        self.assertTrue(synced["sync_acknowledged"])
        self.assertFalse(synced["sync_pending"])
        self.assertEqual("sync", synced["acknowledged_operation"])
        self.assertEqual(
            skill_process._operation_uuid(context, "sync"),
            sync.await_args.kwargs["op_id"],
        )

    async def test_running_cli_artifact_sync_is_projection_not_evidence(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        sync = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "workspace_applied": True,
            "sync_pending": False,
            "sync_acknowledged": True,
            "acknowledged_operation": "sync",
            "artifacts": [{
                "kind": "file",
                "path": "report.md",
                "change": "created",
                "size_bytes": 12,
                "sha256": "e" * 64,
            }],
        })
        with patch.object(
            skill_process,
            "sync_isolated_process_artifacts",
            sync,
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="sync",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id=(
                        "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
                    ),
                ),
            ))

        self.assertEqual("success", result["status"])
        self.assertEqual("report.md", result["artifacts"][0]["path"])
        self.assertNotIn("process_evidence_receipt", result)

    async def test_process_artifact_projection_is_bounded_to_512(self) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        artifacts = [
            {
                "path": f"results/{index}.json",
                "size_bytes": index,
                "sha256": f"{index:064x}",
            }
            for index in range(513)
        ]
        with patch.object(
            skill_process,
            "sync_isolated_process_artifacts",
            AsyncMock(return_value={
                "status": "success",
                "state": "running",
                "workspace_applied": True,
                "sync_pending": False,
                "sync_acknowledged": True,
                "acknowledged_operation": "sync",
                "artifacts": artifacts,
            }),
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="sync",
                process_id=started["process_id"],
                context=context,
            ))

        self.assertEqual(512, len(result["artifacts"]))
        self.assertNotIn("process_evidence_receipt", result)

    async def test_argument_free_cli_terminal_stdout_binds_success_evidence(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        stdout = b"result\n"
        read = AsyncMock(return_value={
            "status": "success",
            "state": "exited",
            "stdout_bytes": stdout,
            "stderr_bytes": b"",
            "stdout_start_offset": 0,
            "stdout_next_offset": len(stdout),
            "stdout_end_offset": len(stdout),
            "stderr_start_offset": 0,
            "stderr_next_offset": 0,
            "stderr_end_offset": 0,
            "stdout_data_loss": False,
            "stderr_data_loss": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout_eof": True,
            "stderr_eof": True,
            "returncode": 0,
        })
        with patch.object(
            skill_process,
            "read_isolated_process_output",
            read,
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id=(
                        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
                    ),
                ),
            ))

        receipt = result["process_evidence_receipt"]
        self.assertEqual("cli_exit", receipt["completion_kind"])
        self.assertEqual("success", receipt["outcome"])
        self.assertEqual(len(stdout), receipt["stdout_size_bytes"])
        self.assertEqual(
            hashlib.sha256(stdout).hexdigest(),
            receipt["stdout_sha256"],
        )

    async def test_argument_free_cli_empty_terminal_output_is_not_evidence(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        read = AsyncMock(return_value={
            "status": "success",
            "state": "exited",
            "stdout_bytes": b"",
            "stderr_bytes": b"",
            "stdout_start_offset": 0,
            "stdout_next_offset": 0,
            "stdout_end_offset": 0,
            "stderr_start_offset": 0,
            "stderr_next_offset": 0,
            "stderr_end_offset": 0,
            "stdout_data_loss": False,
            "stderr_data_loss": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout_eof": True,
            "stderr_eof": True,
            "returncode": 0,
        })
        with patch.object(
            skill_process,
            "read_isolated_process_output",
            read,
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=started["process_id"],
                context=replace(
                    context,
                    tool_operation_id=(
                        "ffffffff-ffff-4fff-8fff-ffffffffffff"
                    ),
                ),
            ))

        self.assertEqual("success", result["status"])
        self.assertNotIn("process_evidence_receipt", result)

    async def test_binary_read_uses_base64_without_duplicate_text(self) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        read = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "stdout_bytes": b"\xff\x00",
            "stderr_bytes": b"",
            **{
                f"{stream}_{suffix}": value
                for stream in ("stdout", "stderr")
                for suffix, value in (
                    ("start_offset", 0),
                    ("next_offset", 2 if stream == "stdout" else 0),
                    ("end_offset", 2 if stream == "stdout" else 0),
                    ("data_loss", False),
                    ("truncated", False),
                    ("eof", False),
                )
            },
        })
        with patch.object(
            skill_process,
            "read_isolated_process_output",
            read,
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=context,
            ))
        self.assertEqual("/wA=", result["stdout_base64"])
        self.assertEqual("base64", result["stdout_encoding"])
        self.assertNotIn("stdout_text", result)

    async def test_dispatch_unknown_fences_every_nonclose_mutation_until_exact_replay(
        self,
    ) -> None:
        context = self.context()
        cases = [
            (
                "write",
                {"data": "payload"},
                "write_isolated_process_stdin",
                {
                    "status": "success",
                    "state": "running",
                    "bytes_written": 7,
                    "requested_bytes": 7,
                    "partial": False,
                },
            ),
            (
                "stdin_close",
                {},
                "close_isolated_process_stdin",
                {
                    "status": "success",
                    "state": "running",
                    "stdin_closed": True,
                    "already_closed": False,
                },
            ),
            (
                "call",
                {
                    "method_name": "query",
                    "method_args": ["term"],
                },
                "call_isolated_process_instance",
                {
                    "status": "success",
                    "state": "running",
                    "method_name": "query",
                    "call_id": skill_process._operation_uuid(
                        context,
                        "call",
                    ),
                    "partial": False,
                },
            ),
            (
                "signal",
                {"signal": "interrupt"},
                "signal_isolated_process",
                {
                    "status": "success",
                    "state": "running",
                    "signal": "interrupt",
                    "signal_delivered": True,
                },
            ),
            (
                "sync",
                {},
                "sync_isolated_process_artifacts",
                {
                    "status": "success",
                    "state": "running",
                    "workspace_applied": True,
                    "sync_pending": False,
                    "sync_acknowledged": True,
                    "acknowledged_operation": "sync",
                    "artifacts": [],
                },
            ),
        ]
        probe_ids = [
            "13131313-1313-4313-8313-131313131313",
            "14141414-1414-4414-8414-141414141414",
            "15151515-1515-4515-8515-151515151515",
            "16161616-1616-4616-8616-161616161616",
            "17171717-1717-4717-8717-171717171717",
        ]
        for index, (
            operation,
            operation_kwargs,
            backend_name,
            success_response,
        ) in enumerate(cases):
            with self.subTest(operation=operation):
                started, _opened, _start = await self.start_process(
                    context=context,
                )
                process_id = started["process_id"]
                backend = AsyncMock(side_effect=[
                    IsolatedSkillExecutorError(
                        "executor_unavailable",
                        "response lost after dispatch",
                        dispatch_unknown=True,
                    ),
                    success_response,
                ])
                probe_context = replace(
                    context,
                    tool_operation_id=probe_ids[index],
                )
                probe_operation = (
                    "write" if operation == "signal" else "signal"
                )
                probe_kwargs = (
                    {"data": "different"}
                    if probe_operation == "write"
                    else {"signal": "terminate"}
                )
                probe_backend_name = (
                    "write_isolated_process_stdin"
                    if probe_operation == "write"
                    else "signal_isolated_process"
                )
                probe_backend = AsyncMock()
                with (
                    patch.object(
                        skill_process,
                        backend_name,
                        backend,
                    ),
                    patch.object(
                        skill_process,
                        probe_backend_name,
                        probe_backend,
                    ),
                ):
                    uncertain = json.loads(
                        await skill_process.run_skill_process(
                            operation=operation,
                            process_id=process_id,
                            context=context,
                            **operation_kwargs,
                        )
                    )
                    blocked = json.loads(
                        await skill_process.run_skill_process(
                            operation=probe_operation,
                            process_id=process_id,
                            context=probe_context,
                            **probe_kwargs,
                        )
                    )
                    reconciled = json.loads(
                        await skill_process.run_skill_process(
                            operation=operation,
                            process_id=process_id,
                            context=context,
                            **operation_kwargs,
                        )
                    )

                self.assertEqual(
                    "executor_unavailable",
                    uncertain["error_code"],
                )
                self.assertEqual(
                    "process_mutation_reconcile_required",
                    blocked["error_code"],
                )
                probe_backend.assert_not_awaited()
                self.assertEqual("success", reconciled["status"])
                self.assertEqual(2, backend.await_count)
                self.assertIsNone(
                    self.manager._records[process_id].mutation_fence
                )

    async def test_dispatch_unknown_close_requires_exact_reconcile(self) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        close_backend = AsyncMock(side_effect=[
            IsolatedSkillExecutorError(
                "executor_unavailable",
                "close response lost",
                dispatch_unknown=True,
            ),
            {
                "status": "success",
                "state": "closed",
                "workspace_applied": True,
                "artifacts": [],
            },
        ])
        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            close_backend,
        ):
            uncertain = json.loads(await skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=context,
            ))
            blocked = json.loads(await skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id=(
                        "18181818-1818-4818-8818-181818181818"
                    ),
                ),
            ))
            reconciled = json.loads(await skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=context,
            ))

        self.assertEqual("executor_unavailable", uncertain["error_code"])
        self.assertEqual(
            "process_mutation_reconcile_required",
            blocked["error_code"],
        )
        self.assertEqual("success", reconciled["status"])
        self.assertEqual(2, close_backend.await_count)

    async def test_post_dispatch_cancellation_fences_persistent_mutation(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        cancelled = isolated_executor._ProcessDispatchCancelled(
            "fixture cancellation after UDS write"
        )

        with patch.object(
            skill_process,
            "write_isolated_process_stdin",
            AsyncMock(side_effect=cancelled),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await skill_process.run_skill_process(
                    operation="write",
                    process_id=process_id,
                    data="payload",
                    context=context,
                )

        record = self.manager._records[process_id]
        self.assertIsNotNone(record.mutation_fence)
        different_context = replace(
            context,
            tool_operation_id="19191919-1919-4919-8919-191919191919",
        )
        with patch.object(
            skill_process,
            "signal_isolated_process",
            AsyncMock(),
        ) as signal_backend:
            blocked = json.loads(await skill_process.run_skill_process(
                operation="signal",
                process_id=process_id,
                signal="terminate",
                context=different_context,
            ))

        self.assertEqual(
            "process_mutation_reconcile_required",
            blocked["error_code"],
        )
        signal_backend.assert_not_awaited()

    async def test_cleanup_may_terminate_a_mutation_fenced_process(self) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        with patch.object(
            skill_process,
            "write_isolated_process_stdin",
            AsyncMock(side_effect=IsolatedSkillExecutorError(
                "executor_unavailable",
                "write response lost",
                dispatch_unknown=True,
            )),
        ):
            uncertain = json.loads(await skill_process.run_skill_process(
                operation="write",
                process_id=process_id,
                data="payload",
                context=context,
            ))
        close_backend = AsyncMock(return_value={
            "status": "success",
            "state": "closed",
            "workspace_applied": False,
            "artifacts": [],
        })
        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            close_backend,
        ):
            cleanup = await self.manager.cleanup_root(
                context.user_id,
                context.session_id,
                str(context.root_run_id),
            )

        self.assertEqual("executor_unavailable", uncertain["error_code"])
        self.assertTrue(cleanup["success"])
        self.assertFalse(self.manager._records)
        close_backend.assert_awaited_once()

    async def test_python_class_and_factory_call_modes_are_exact(self) -> None:
        python_script = self.skill / "scripts" / "operator.py"
        python_script.write_text(
            "class BrowserSession:\n"
            "    def snapshot(self, value=None):\n"
            "        return value\n\n"
            "def open_browser():\n"
            "    return BrowserSession()\n",
            encoding="utf-8",
        )
        old_script = self.script
        self.script = python_script
        try:
            context = self.context(script=python_script)
            class_result, opened, _started = await self.start_process(
                context=context,
                script_path="skills/portable-process/scripts/operator.py",
                invocation_mode="instance",
                class_name="BrowserSession",
            )
            self.assertEqual(
                "BrowserSession",
                opened.await_args.kwargs["class_name"],
            )
            call = AsyncMock(return_value={
                "status": "success",
                "state": "running",
                "method_name": "snapshot",
                "call_id": skill_process._operation_uuid(context, "call"),
                "partial": False,
            })
            with patch.object(
                skill_process,
                "call_isolated_process_instance",
                call,
            ):
                called = json.loads(await skill_process.run_skill_process(
                    operation="call",
                    process_id=class_result["process_id"],
                    method_name="snapshot",
                    method_args=[{"full": True}],
                    context=context,
                ))
            self.assertEqual("success", called["status"])
            self.assertTrue(called["call_enqueued"])
            self.assertEqual("stdout_jsonl", called["result_delivery"])
            self.assertEqual(
                skill_process._operation_uuid(context, "call"),
                called["call_id"],
            )
            self.assertIn("matching call_result", called["next_action"])
            self.assertIn("sync", called["next_action"])
            self.assertEqual([{"full": True}], call.await_args.kwargs["method_args"])

            factory_result, opened, _started = await self.start_process(
                context=context,
                script_path="skills/portable-process/scripts/operator.py",
                runtime_profile="base-v1",
                invocation_mode="factory",
                factory_name="open_browser",
            )
            self.assertEqual(
                "open_browser",
                opened.await_args.kwargs["factory_name"],
            )
            self.assertEqual("factory", factory_result["invocation_mode"])
        finally:
            self.script = old_script

    async def test_close_is_idempotent_and_restart_loses_unpersisted_handle(self) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        close = AsyncMock(return_value={
            "status": "success",
            "state": "closed",
            "workspace_applied": True,
            "artifacts": [],
        })
        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            close,
        ):
            first = json.loads(await skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=context,
            ))
            second = json.loads(await skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=context,
            ))
        self.assertEqual("success", first["status"])
        self.assertTrue(second["already_closed"])
        close.assert_awaited_once()

        result, _opened, _start = await self.start_process(context=context)
        skill_process._MANAGER = skill_process.SkillProcessManager()
        lost = json.loads(await skill_process.run_skill_process(
            operation="read",
            process_id=result["process_id"],
            context=context,
        ))
        self.assertEqual("lease_lost", lost["error_code"])

    async def test_executor_lost_lease_removes_stale_manager_record(self) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        read = AsyncMock(side_effect=IsolatedSkillExecutorError(
            "lease_not_found",
            "gone",
        ))
        with patch.object(
            skill_process,
            "read_isolated_process_output",
            read,
        ):
            first = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=context,
            ))
            second = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=context,
            ))
        self.assertEqual("lease_lost", first["error_code"])
        self.assertEqual("lease_lost", second["error_code"])
        read.assert_awaited_once()

    async def test_terminal_lease_errors_finalize_slot_before_manager_forgets(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        record = self.manager._records[process_id]
        released = _FakeSlotReservation()
        record.lease._slot_reservation = released

        with patch.object(
            skill_process,
            "read_isolated_process_output",
            AsyncMock(side_effect=IsolatedSkillExecutorError(
                "lease_lost",
                "executor no longer retains this lease",
            )),
        ):
            lost = json.loads(await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                context=context,
            ))

        self.assertEqual("lease_lost", lost["error_code"])
        self.assertEqual([("release", None)], released.actions)
        self.assertNotIn(process_id, self.manager._records)

        second, _opened, _start = await self.start_process(context=context)
        second_id = second["process_id"]
        second_record = self.manager._records[second_id]
        quarantined = _FakeSlotReservation()
        second_record.lease._slot_reservation = quarantined
        terminal_error = IsolatedSkillExecutorError(
            "worker_containment_failed",
            "closed tree could not be removed",
            terminal_lease_state="quarantined",
        )
        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            AsyncMock(side_effect=terminal_error),
        ):
            cleanup = await self.manager.cleanup_root(
                context.user_id,
                context.session_id,
                str(context.root_run_id),
            )

        self.assertTrue(cleanup["success"])
        self.assertEqual(1, cleanup["closed"])
        self.assertEqual(1, cleanup["terminal_lost"])
        self.assertEqual(
            [(
                "quarantine",
                "worker_containment_failed",
            )],
            quarantined.actions,
        )
        self.assertNotIn(second_id, self.manager._records)

    async def test_terminal_closed_artifact_error_is_typed_and_releases_slot(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        record = self.manager._records[process_id]
        reservation = _FakeSlotReservation()
        record.lease._slot_reservation = reservation
        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            AsyncMock(side_effect=IsolatedSkillExecutorError(
                "close_artifact_collection_failed",
                "unsafe artifact batch discarded",
                terminal_lease_state="closed",
            )),
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=context,
            ))

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "close_artifact_collection_failed",
            result["error_code"],
        )
        self.assertEqual([("release", None)], reservation.actions)
        self.assertNotIn(process_id, self.manager._records)

    async def test_pending_apply_intent_rejects_both_retry_directions(
        self,
    ) -> None:
        context = self.context()
        for first_intent, retry_intent in ((False, True), (True, False)):
            with self.subTest(
                first_intent=first_intent,
                retry_intent=retry_intent,
            ):
                lease = self.fake_lease(context=context)
                operations: list[str] = []

                async def execute(
                    _lease,
                    operation,
                    *,
                    op_id=None,
                    extra=None,
                    timeout=10,
                ):
                    del _lease, op_id, extra, timeout
                    operations.append(operation)
                    if operation == "close":
                        return {
                            "status": "success",
                            "state": "closing",
                            "sync_token": "t" * 43,
                        }, []
                    raise IsolatedSkillExecutorError(
                        "executor_unavailable",
                        "injected ACK failure",
                    )

                with patch.object(
                    isolated_executor,
                    "_execute_process_operation",
                    side_effect=execute,
                ):
                    with self.assertRaises(
                        IsolatedSkillExecutorError,
                    ) as first:
                        await isolated_executor.close_isolated_process_lease(
                            lease,
                            apply_artifacts=first_intent,
                        )
                    with self.assertRaises(
                        IsolatedSkillExecutorError,
                    ) as mismatch:
                        await isolated_executor.close_isolated_process_lease(
                            lease,
                            apply_artifacts=retry_intent,
                        )

                self.assertEqual(
                    "executor_unavailable",
                    first.exception.code,
                )
                self.assertEqual(
                    "pending_sync_intent_mismatch",
                    mismatch.exception.code,
                )
                self.assertEqual(["close", "ack"], operations)

    async def test_session_cleanup_discards_unapplied_pending_close(
        self,
    ) -> None:
        context = self.context()
        lease = self.fake_lease(context=context)
        content = b"must-not-cross-deletion-fence"
        metadata = {
            "path": "late.txt",
            "change": "created",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        operations: list[str] = []

        async def execute(
            _lease,
            operation,
            *,
            op_id=None,
            extra=None,
            timeout=10,
        ):
            del _lease, op_id, extra, timeout
            operations.append(operation)
            if operation == "close":
                return {
                    "status": "success",
                    "state": "closing",
                    "sync_token": "d" * 43,
                }, [("late.txt", content, metadata)]
            return {
                "status": "success",
                "state": "closed",
                "acknowledged_operation": "close",
            }, []

        with (
            patch.object(
                isolated_executor,
                "_execute_process_operation",
                side_effect=execute,
            ),
            patch.object(
                isolated_executor,
                "apply_artifacts_atomically",
                side_effect=IsolatedSkillExecutorError(
                    "workspace_session_deleted",
                    "The deletion tombstone rejected the apply.",
                ),
            ) as apply_batch,
        ):
            with self.assertRaises(IsolatedSkillExecutorError):
                await isolated_executor.close_isolated_process_lease(
                    lease,
                    apply_artifacts=True,
                )
            closed = await isolated_executor.close_isolated_process_lease(
                lease,
                apply_artifacts=False,
                discard_pending_artifacts=True,
            )

        self.assertEqual("closed", closed["state"])
        self.assertFalse(closed["workspace_applied"])
        self.assertEqual([], closed["artifacts"])
        self.assertTrue(lease.closed)
        apply_batch.assert_called_once()
        self.assertEqual(["close", "ack"], operations)

    async def test_session_cleanup_close_does_not_cross_real_tombstone(
        self,
    ) -> None:
        context = self.context()
        lease = self.fake_lease(context=context)
        workspace = (
            self.root
            / "sandbox"
            / context.user_id
            / context.session_id
            / "workspace"
        )
        workspace.mkdir(parents=True)
        lease._workspace = workspace
        tombstone = (
            workspace.parent.parent
            / ".chatds-session-tombstones"
            / f"{context.session_id}.deleted"
        )
        tombstone.parent.mkdir(mode=0o700)
        tombstone.write_text(
            "chatds-session-deletion-v2\nboot_id=test\n",
            encoding="ascii",
        )
        tombstone.chmod(0o600)

        content = b"must-be-discarded"
        metadata = {
            "path": "blocked.txt",
            "change": "created",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        operations: list[str] = []

        async def execute(
            _lease,
            operation,
            *,
            op_id=None,
            extra=None,
            timeout=10,
        ):
            del _lease, op_id, extra, timeout
            operations.append(operation)
            if operation == "close":
                return {
                    "status": "success",
                    "state": "closing",
                    "sync_token": "r" * 43,
                }, [("blocked.txt", content, metadata)]
            return {
                "status": "success",
                "state": "closed",
                "acknowledged_operation": "close",
            }, []

        with patch.object(
            isolated_executor,
            "_execute_process_operation",
            side_effect=execute,
        ):
            with self.assertRaises(WorkspaceMutationLockError) as blocked:
                await isolated_executor.close_isolated_process_lease(
                    lease,
                    apply_artifacts=True,
                )
            closed = await isolated_executor.close_isolated_process_lease(
                lease,
                apply_artifacts=False,
                discard_pending_artifacts=True,
            )

        self.assertEqual(
            "workspace_session_deleted",
            blocked.exception.code,
        )
        self.assertEqual("closed", closed["state"])
        self.assertFalse(closed["workspace_applied"])
        self.assertFalse((workspace / "blocked.txt").exists())
        self.assertEqual(["close", "ack"], operations)

    async def test_session_cleanup_discards_pending_sync_then_closes(
        self,
    ) -> None:
        context = self.context()
        lease = self.fake_lease(context=context)
        content = b"pending-sync-must-be-discarded"
        metadata = {
            "path": "sync-late.txt",
            "change": "created",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        operations: list[str] = []
        ack_count = 0

        async def execute(
            _lease,
            operation,
            *,
            op_id=None,
            extra=None,
            timeout=10,
        ):
            nonlocal ack_count
            del _lease, op_id, extra, timeout
            operations.append(operation)
            if operation == "sync":
                return {
                    "status": "success",
                    "state": "running",
                    "sync_token": "s" * 43,
                }, [("sync-late.txt", content, metadata)]
            if operation == "close":
                return {
                    "status": "success",
                    "state": "closing",
                    "sync_token": "c" * 43,
                }, []
            ack_count += 1
            return {
                "status": "success",
                "state": "running" if ack_count == 1 else "closed",
                "acknowledged_operation": (
                    "sync" if ack_count == 1 else "close"
                ),
            }, []

        with (
            patch.object(
                isolated_executor,
                "_execute_process_operation",
                side_effect=execute,
            ),
            patch.object(
                isolated_executor,
                "apply_artifacts_atomically",
                side_effect=IsolatedSkillExecutorError(
                    "workspace_session_deleted",
                    "The deletion tombstone rejected the apply.",
                ),
            ) as apply_batch,
        ):
            with self.assertRaises(IsolatedSkillExecutorError):
                await isolated_executor.sync_isolated_process_artifacts(
                    lease,
                )
            closed = await isolated_executor.close_isolated_process_lease(
                lease,
                apply_artifacts=False,
                discard_pending_artifacts=True,
            )

        self.assertEqual("closed", closed["state"])
        self.assertFalse(closed["workspace_applied"])
        self.assertTrue(lease.closed)
        apply_batch.assert_called_once()
        self.assertEqual(["sync", "ack", "close", "ack"], operations)

    async def test_apply_and_ack_failures_retry_exact_pending_close_once(
        self,
    ) -> None:
        context = self.context()
        content = b"transactional-artifact"
        metadata = {
            "path": "transaction.txt",
            "change": "created",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

        apply_lease = self.fake_lease(context=context)
        apply_operations: list[tuple[str, str | None]] = []

        async def execute_apply(
            _lease,
            operation,
            *,
            op_id=None,
            extra=None,
            timeout=10,
        ):
            del _lease, extra, timeout
            apply_operations.append((operation, op_id))
            if operation == "close":
                return {
                    "status": "success",
                    "state": "closing",
                    "sync_token": "a" * 43,
                }, [("transaction.txt", content, metadata)]
            return {
                "status": "success",
                "state": "closed",
                "acknowledged_operation": "close",
            }, []

        applied_manifest = [{
            "path": "transaction.txt",
            "change": "created",
            "size_bytes": len(content),
            "sha256": metadata["sha256"],
        }]
        with (
            patch.object(
                isolated_executor,
                "_execute_process_operation",
                side_effect=execute_apply,
            ),
            patch.object(
                isolated_executor,
                "apply_artifacts_atomically",
                side_effect=[
                    IsolatedSkillExecutorError(
                        "artifact_apply_failed",
                        "injected atomic apply failure",
                    ),
                    applied_manifest,
                ],
            ) as apply_batch,
        ):
            with self.assertRaises(IsolatedSkillExecutorError):
                await isolated_executor.close_isolated_process_lease(
                    apply_lease,
                    apply_artifacts=True,
                )
            applied = await isolated_executor.close_isolated_process_lease(
                apply_lease,
                apply_artifacts=True,
            )

        self.assertEqual("closed", applied["state"])
        self.assertEqual(2, apply_batch.call_count)
        self.assertEqual(
            ["close", "ack"],
            [operation for operation, _op_id in apply_operations],
        )

        ack_lease = self.fake_lease(context=context)
        ack_operations: list[tuple[str, str | None]] = []
        ack_attempts = 0

        async def execute_ack(
            _lease,
            operation,
            *,
            op_id=None,
            extra=None,
            timeout=10,
        ):
            nonlocal ack_attempts
            del _lease, extra, timeout
            ack_operations.append((operation, op_id))
            if operation == "close":
                return {
                    "status": "success",
                    "state": "closing",
                    "sync_token": "b" * 43,
                }, [("transaction.txt", content, metadata)]
            ack_attempts += 1
            if ack_attempts == 1:
                raise IsolatedSkillExecutorError(
                    "executor_unavailable",
                    "injected ACK response loss",
                )
            return {
                "status": "success",
                "state": "closed",
                "acknowledged_operation": "close",
            }, []

        with (
            patch.object(
                isolated_executor,
                "_execute_process_operation",
                side_effect=execute_ack,
            ),
            patch.object(
                isolated_executor,
                "apply_artifacts_atomically",
                return_value=applied_manifest,
            ) as apply_once,
        ):
            with self.assertRaises(IsolatedSkillExecutorError):
                await isolated_executor.close_isolated_process_lease(
                    ack_lease,
                    apply_artifacts=True,
                )
            acknowledged = (
                await isolated_executor.close_isolated_process_lease(
                    ack_lease,
                    apply_artifacts=True,
                )
            )

        self.assertEqual("closed", acknowledged["state"])
        apply_once.assert_called_once()
        ack_ids = [
            op_id
            for operation, op_id in ack_operations
            if operation == "ack"
        ]
        self.assertEqual(2, len(ack_ids))
        self.assertEqual(ack_ids[0], ack_ids[1])

    async def test_cleanup_failure_retains_record_for_exact_retry(self) -> None:
        context = self.context()
        result, _opened, _started = await self.start_process(context=context)
        process_id = result["process_id"]
        close = AsyncMock(side_effect=[
            IsolatedSkillExecutorError(
                "workspace_conflict",
                "The pending workspace CAS could not be applied.",
            ),
            {
                "status": "success",
                "state": "closed",
                "artifacts": [],
            },
        ])
        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            close,
        ):
            failed = await self.manager.cleanup_root(
                context.user_id,
                context.session_id,
                str(context.root_run_id),
            )
            self.assertFalse(failed["success"])
            self.assertEqual(1, failed["retained_for_retry"])
            retained = await self.manager.acquire(process_id, context)
            self.assertEqual(process_id, retained.process_id)

            retried = await self.manager.cleanup_root(
                context.user_id,
                context.session_id,
                str(context.root_run_id),
            )
        self.assertTrue(retried["success"])
        self.assertEqual(1, retried["closed"])
        self.assertEqual(2, close.await_count)
        with self.assertRaises(IsolatedSkillExecutorError) as closed:
            await self.manager.acquire(process_id, context)
        self.assertEqual("process_closed", closed.exception.code)

    async def test_waiting_start_does_not_block_close_releasing_pool_slot(
        self,
    ) -> None:
        first_context = self.context()
        first, _opened, _started = await self.start_process(
            context=first_context,
        )
        first_record = self.manager._records[first["process_id"]]
        second_context = replace(
            first_context,
            tool_operation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        second_lease = self.fake_lease(context=second_context)
        open_waiting = asyncio.Event()
        slot_released = asyncio.Event()

        async def open_second(**_kwargs):
            open_waiting.set()
            await slot_released.wait()
            return second_lease, {
                "status": "success",
                "state": "open",
            }

        async def close_first(lease, **_kwargs):
            self.assertIs(first_record.lease, lease)
            slot_released.set()
            return {
                "status": "success",
                "state": "closed",
                "workspace_applied": True,
                "artifacts": [],
            }

        start_second = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "session-sandbox-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "none",
            },
        })
        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                side_effect=open_second,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                start_second,
            ),
            patch.object(
                skill_process,
                "close_isolated_process_lease",
                side_effect=close_first,
            ),
        ):
            waiting_task = asyncio.create_task(
                skill_process.run_skill_process(
                    operation="start",
                    script_path=(
                        "skills/portable-process/scripts/session.cjs"
                    ),
                    context=second_context,
                )
            )
            await asyncio.wait_for(open_waiting.wait(), timeout=1)
            close_result = json.loads(await asyncio.wait_for(
                skill_process.run_skill_process(
                    operation="close",
                    process_id=first["process_id"],
                    context=first_context,
                ),
                timeout=1,
            ))
            second_result = json.loads(await asyncio.wait_for(
                waiting_task,
                timeout=1,
            ))

        self.assertEqual("success", close_result["status"])
        self.assertEqual("success", second_result["status"])
        self.assertNotEqual(
            first["process_id"],
            second_result["process_id"],
        )
        start_second.assert_awaited_once()
        self.assertFalse(self.manager._pending_starts)

    async def test_cleanup_cancels_start_while_it_is_only_waiting_admission(
        self,
    ) -> None:
        context = self.context()
        open_waiting = asyncio.Event()

        async def wait_for_admission_cancel(**kwargs):
            cancel_event = kwargs["admission_cancel_event"]
            open_waiting.set()
            await cancel_event.wait()
            raise IsolatedSkillExecutorError(
                "executor_admission_cancelled",
                "cancelled before reservation",
            )

        start_lease = AsyncMock()
        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                side_effect=wait_for_admission_cancel,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                start_lease,
            ),
        ):
            waiting_task = asyncio.create_task(
                skill_process.run_skill_process(
                    operation="start",
                    script_path=(
                        "skills/portable-process/scripts/session.cjs"
                    ),
                    context=context,
                )
            )
            await asyncio.wait_for(open_waiting.wait(), timeout=1)
            cleanup = await asyncio.wait_for(
                self.manager.cleanup_root(
                    context.user_id,
                    context.session_id,
                    str(context.root_run_id),
                ),
                timeout=1,
            )
            result = json.loads(await asyncio.wait_for(
                waiting_task,
                timeout=1,
            ))

        self.assertTrue(cleanup["success"])
        self.assertEqual(0, cleanup["matched"])
        self.assertEqual(1, cleanup["pending_starts_cancelled"])
        self.assertEqual(0, cleanup["pending_starts_timed_out"])
        self.assertEqual(
            "process_start_cancelled",
            result["error_code"],
        )
        start_lease.assert_not_awaited()
        self.assertFalse(self.manager._pending_starts)

    async def test_cleanup_bounds_unresponsive_pending_start_wait(self) -> None:
        context = self.context()
        open_waiting = asyncio.Event()
        never = asyncio.Event()

        async def unresponsive_open(**_kwargs):
            open_waiting.set()
            await never.wait()
            raise IsolatedSkillExecutorError(
                "executor_unavailable",
                "bounded test release",
            )

        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                side_effect=unresponsive_open,
            ),
            patch.object(
                skill_process,
                "MAX_PENDING_START_CLEANUP_WAIT_SECONDS",
                0.01,
            ),
        ):
            waiting_task = asyncio.create_task(
                skill_process.run_skill_process(
                    operation="start",
                    script_path=(
                        "skills/portable-process/scripts/session.cjs"
                    ),
                    context=context,
                )
            )
            await asyncio.wait_for(open_waiting.wait(), timeout=1)
            cleanup = await asyncio.wait_for(
                self.manager.cleanup_root(
                    context.user_id,
                    context.session_id,
                    str(context.root_run_id),
                ),
                timeout=1,
            )
            never.set()
            waiting_result = json.loads(await asyncio.wait_for(
                waiting_task,
                timeout=1,
            ))

        self.assertFalse(cleanup["success"])
        self.assertEqual(1, cleanup["pending_starts_timed_out"])
        self.assertEqual(
            "process_start_cleanup_timeout",
            cleanup["failures"][0]["error_code"],
        )
        self.assertEqual(
            "executor_unavailable",
            waiting_result["error_code"],
        )
        self.assertFalse(self.manager._pending_starts)

    async def test_caller_cancel_after_admission_completes_open_then_rolls_back(
        self,
    ) -> None:
        context = self.context()
        lease = self.fake_lease(context=context)
        uds_started = asyncio.Event()
        finish_open = asyncio.Event()

        async def granted_open(**_kwargs):
            uds_started.set()
            await finish_open.wait()
            return lease, {
                "status": "success",
                "state": "open",
            }

        rollback = AsyncMock(return_value={
            "status": "success",
            "state": "closed",
            "workspace_applied": False,
            "artifacts": [],
        })
        start_backend = AsyncMock()
        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                side_effect=granted_open,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                start_backend,
            ),
            patch.object(
                skill_process,
                "close_isolated_process_lease",
                rollback,
            ),
        ):
            caller = asyncio.create_task(skill_process.run_skill_process(
                operation="start",
                script_path=(
                    "skills/portable-process/scripts/session.cjs"
                ),
                context=context,
            ))
            await asyncio.wait_for(uds_started.wait(), timeout=1)
            caller.cancel()
            await asyncio.sleep(0)
            self.assertFalse(caller.done())
            finish_open.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=1)

        start_backend.assert_not_awaited()
        rollback.assert_awaited_once()
        self.assertFalse(self.manager._pending_starts)
        self.assertFalse(self.manager._records)

    async def test_cleanup_cancels_waiting_start_without_orphaning_lease(
        self,
    ) -> None:
        first_context = self.context()
        first, _opened, _started = await self.start_process(
            context=first_context,
        )
        first_record = self.manager._records[first["process_id"]]
        second_context = replace(
            first_context,
            tool_operation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        )
        second_lease = self.fake_lease(context=second_context)
        open_waiting = asyncio.Event()
        slot_released = asyncio.Event()
        closed_leases: list[IsolatedProcessLease] = []

        async def open_second(**_kwargs):
            open_waiting.set()
            await slot_released.wait()
            return second_lease, {
                "status": "success",
                "state": "open",
            }

        async def close_lease(lease, **_kwargs):
            closed_leases.append(lease)
            if lease is first_record.lease:
                slot_released.set()
            return {
                "status": "success",
                "state": "closed",
                "workspace_applied": False,
                "artifacts": [],
            }

        start_second = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "session-sandbox-v1",
        })
        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                side_effect=open_second,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                start_second,
            ),
            patch.object(
                skill_process,
                "close_isolated_process_lease",
                side_effect=close_lease,
            ),
        ):
            waiting_task = asyncio.create_task(
                skill_process.run_skill_process(
                    operation="start",
                    script_path=(
                        "skills/portable-process/scripts/session.cjs"
                    ),
                    context=second_context,
                )
            )
            await asyncio.wait_for(open_waiting.wait(), timeout=1)
            cleanup_result = await asyncio.wait_for(
                self.manager.cleanup_root(
                    first_context.user_id,
                    first_context.session_id,
                    str(first_context.root_run_id),
                ),
                timeout=1,
            )
            waiting_result = json.loads(await asyncio.wait_for(
                waiting_task,
                timeout=1,
            ))

        self.assertTrue(cleanup_result["success"])
        self.assertEqual(1, cleanup_result["matched"])
        self.assertEqual(1, cleanup_result["pending_starts_cancelled"])
        self.assertEqual("error", waiting_result["status"])
        self.assertEqual(
            "process_start_cancelled",
            waiting_result["error_code"],
        )
        start_second.assert_not_awaited()
        self.assertEqual(
            [first_record.lease, second_lease],
            closed_leases,
        )
        self.assertFalse(self.manager._records)
        self.assertFalse(self.manager._pending_starts)

    async def test_explicit_close_callers_join_one_runtime_transaction(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        close_entered = asyncio.Event()
        release_close = asyncio.Event()

        async def close_once(_lease, **_kwargs):
            close_entered.set()
            await release_close.wait()
            return {
                "status": "success",
                "state": "closed",
                "workspace_applied": True,
                "artifacts": [],
            }

        close_backend = AsyncMock(side_effect=close_once)
        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            close_backend,
        ):
            first = asyncio.create_task(skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=context,
            ))
            await asyncio.wait_for(close_entered.wait(), timeout=1)
            second = asyncio.create_task(skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=replace(
                    context,
                    tool_operation_id=(
                        "12121212-1212-4212-8212-121212121212"
                    ),
                ),
            ))
            await asyncio.sleep(0)
            self.assertEqual(1, close_backend.await_count)
            release_close.set()
            first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual("success", json.loads(first_result)["status"])
        self.assertEqual("success", json.loads(second_result)["status"])
        close_backend.assert_awaited_once()
        self.assertFalse(self.manager._records)

    async def test_cancelled_close_waiter_does_not_cancel_runtime_close(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        close_entered = asyncio.Event()
        release_close = asyncio.Event()
        close_finished = asyncio.Event()

        async def close_once(_lease, **_kwargs):
            close_entered.set()
            await release_close.wait()
            close_finished.set()
            return {
                "status": "success",
                "state": "closed",
                "workspace_applied": True,
                "artifacts": [],
            }

        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            side_effect=close_once,
        ):
            caller = asyncio.create_task(skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=context,
            ))
            await asyncio.wait_for(close_entered.wait(), timeout=1)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            release_close.set()
            await asyncio.wait_for(close_finished.wait(), timeout=1)
            await asyncio.sleep(0)
            replay = json.loads(await skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=context,
            ))

        self.assertEqual("success", replay["status"])
        self.assertTrue(replay["already_closed"])

    async def test_cleanup_joins_an_explicit_close_already_in_progress(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        close_entered = asyncio.Event()
        release_close = asyncio.Event()

        async def close_once(_lease, **_kwargs):
            close_entered.set()
            await release_close.wait()
            return {
                "status": "success",
                "state": "closed",
                "workspace_applied": True,
                "artifacts": [],
            }

        close_backend = AsyncMock(side_effect=close_once)
        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            close_backend,
        ):
            explicit = asyncio.create_task(skill_process.run_skill_process(
                operation="close",
                process_id=process_id,
                context=context,
            ))
            await asyncio.wait_for(close_entered.wait(), timeout=1)
            cleanup_task = asyncio.create_task(self.manager.cleanup_root(
                context.user_id,
                context.session_id,
                str(context.root_run_id),
            ))
            await asyncio.sleep(0)
            self.assertFalse(cleanup_task.done())
            release_close.set()
            explicit_result, cleanup = await asyncio.gather(
                explicit,
                cleanup_task,
            )

        self.assertEqual("success", json.loads(explicit_result)["status"])
        self.assertTrue(cleanup["success"])
        self.assertEqual(1, cleanup["matched"])
        close_backend.assert_awaited_once()

    async def test_session_cleanup_supersedes_failed_applying_close(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        process_id = started["process_id"]
        close_entered = asyncio.Event()
        release_applying_close = asyncio.Event()
        calls: list[tuple[bool, bool]] = []

        async def close_for_delete(_lease, **kwargs):
            calls.append((
                bool(kwargs.get("apply_artifacts")),
                bool(kwargs.get("discard_pending_artifacts")),
            ))
            if len(calls) == 1:
                close_entered.set()
                await release_applying_close.wait()
                raise IsolatedSkillExecutorError(
                    "workspace_session_deleted",
                    "The durable tombstone rejected artifact apply.",
                )
            return {
                "status": "success",
                "state": "closed",
                "workspace_applied": False,
                "artifacts": [],
            }

        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            side_effect=close_for_delete,
        ):
            explicit = asyncio.create_task(
                skill_process.run_skill_process(
                    operation="close",
                    process_id=process_id,
                    context=context,
                )
            )
            await asyncio.wait_for(close_entered.wait(), timeout=1)
            cleanup_task = asyncio.create_task(
                self.manager.cleanup_session(
                    context.user_id,
                    context.session_id,
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(cleanup_task.done())
            release_applying_close.set()
            explicit_result, cleanup = await asyncio.gather(
                explicit,
                cleanup_task,
            )

        self.assertEqual(
            "workspace_session_deleted",
            json.loads(explicit_result)["error_code"],
        )
        self.assertTrue(cleanup["success"])
        self.assertEqual(1, cleanup["closed"])
        self.assertFalse(self.manager._records)
        self.assertEqual(
            [(True, False), (False, True)],
            calls,
        )

    async def test_cleanup_close_wait_is_bounded_without_assuming_success(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        record = self.manager._records[started["process_id"]]
        close_entered = asyncio.Event()
        release_close = asyncio.Event()

        async def slow_close(_lease, **_kwargs):
            close_entered.set()
            await release_close.wait()
            return {
                "status": "success",
                "state": "closed",
                "workspace_applied": False,
                "artifacts": [],
            }

        with (
            patch.object(
                skill_process,
                "close_isolated_process_lease",
                side_effect=slow_close,
            ),
            patch.object(
                skill_process,
                "MAX_PROCESS_CLOSE_CLEANUP_WAIT_SECONDS",
                0.01,
            ),
        ):
            cleanup = await self.manager.cleanup_root(
                context.user_id,
                context.session_id,
                str(context.root_run_id),
            )
            await asyncio.wait_for(close_entered.wait(), timeout=1)
            self.assertFalse(cleanup["success"])
            self.assertEqual(
                "process_close_cleanup_timeout",
                cleanup["failures"][0]["error_code"],
            )
            self.assertEqual("closing", record.lifecycle_state)
            self.assertIsNotNone(record.close_task)
            release_close.set()
            await asyncio.wait_for(
                asyncio.shield(record.close_task),
                timeout=1,
            )

        self.assertEqual("closed", record.lifecycle_state)
        self.assertFalse(self.manager._records)

    async def test_cancelled_cleanup_does_not_leave_record_permanently_closing(
        self,
    ) -> None:
        context = self.context()
        started, _opened, _start = await self.start_process(context=context)
        record = self.manager._records[started["process_id"]]
        close_entered = asyncio.Event()
        release_close = asyncio.Event()

        async def slow_close(_lease, **_kwargs):
            close_entered.set()
            await release_close.wait()
            return {
                "status": "success",
                "state": "closed",
                "workspace_applied": False,
                "artifacts": [],
            }

        with patch.object(
            skill_process,
            "close_isolated_process_lease",
            side_effect=slow_close,
        ):
            cleanup_task = asyncio.create_task(self.manager.cleanup_root(
                context.user_id,
                context.session_id,
                str(context.root_run_id),
            ))
            await asyncio.wait_for(close_entered.wait(), timeout=1)
            cleanup_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cleanup_task
            self.assertEqual("closing", record.lifecycle_state)
            release_close.set()
            await asyncio.wait_for(
                asyncio.shield(record.close_task),
                timeout=1,
            )

        self.assertEqual("closed", record.lifecycle_state)
        self.assertFalse(self.manager._records)

    async def test_failed_start_with_failed_rollback_is_retained_for_cleanup(
        self,
    ) -> None:
        context = self.context()
        lease = self.fake_lease(context=context)
        open_lease = AsyncMock(return_value=(
            lease,
            {
                "status": "success",
                "state": "open",
                "runtime_profile": "session-sandbox-v1",
                "network_policy": {
                    "direct": "disabled",
                    "egress": "none",
                },
            },
        ))
        start_lease = AsyncMock(side_effect=IsolatedSkillExecutorError(
            "process_start_failed",
            "The exact Skill process could not be started.",
        ))
        close_lease = AsyncMock(side_effect=[
            IsolatedSkillExecutorError(
                "executor_unavailable",
                "The rollback response was unavailable.",
            ),
            {
                "status": "success",
                "state": "closed",
                "artifacts": [],
            },
        ])
        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                open_lease,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                start_lease,
            ),
            patch.object(
                skill_process,
                "close_isolated_process_lease",
                close_lease,
            ),
        ):
            failed = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path=(
                    "skills/portable-process/scripts/session.cjs"
                ),
                context=context,
            ))
            self.assertEqual("process_start_failed", failed["error_code"])
            self.assertNotIn("process_id", failed)
            self.assertEqual(1, len(self.manager._records))

            cleanup = await self.manager.cleanup_root(
                context.user_id,
                context.session_id,
                str(context.root_run_id),
            )

        self.assertTrue(cleanup["success"])
        self.assertEqual(1, cleanup["closed"])
        self.assertFalse(self.manager._records)
        self.assertEqual(2, close_lease.await_count)

    async def test_redispatched_open_reuses_runtime_owner_scope(self) -> None:
        context = self.context()
        lease = self.fake_lease(context=context)
        open_lease = AsyncMock(side_effect=[
            IsolatedSkillExecutorError(
                "executor_unavailable",
                "Both responses were unavailable.",
            ),
            (
                lease,
                {
                    "status": "success",
                    "state": "open",
                    "runtime_profile": "session-sandbox-v1",
                    "network_policy": {
                        "direct": "disabled",
                        "egress": "none",
                    },
                },
            ),
        ])
        start_lease = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "session-sandbox-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "none",
            },
        })
        with (
            patch.object(
                skill_process,
                "open_isolated_process_lease",
                open_lease,
            ),
            patch.object(
                skill_process,
                "start_isolated_process_lease",
                start_lease,
            ),
        ):
            first = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path=(
                    "skills/portable-process/scripts/session.cjs"
                ),
                context=context,
            ))
            second = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path=(
                    "skills/portable-process/scripts/session.cjs"
                ),
                context=context,
            ))

        self.assertEqual("executor_unavailable", first["error_code"])
        self.assertEqual("success", second["status"])
        first_scope = open_lease.await_args_list[0].kwargs["owner_scope"]
        second_scope = open_lease.await_args_list[1].kwargs["owner_scope"]
        self.assertIs(first_scope, second_scope)
        self.assertEqual(
            open_lease.await_args_list[0].kwargs["op_id"],
            open_lease.await_args_list[1].kwargs["op_id"],
        )

    def test_agent_loop_operation_id_is_stable_and_scope_separated(self) -> None:
        context = self.context()
        first = agent_loop._tool_dispatch_context(context, "call-1")
        retry = agent_loop._tool_dispatch_context(context, "call-1")
        other_call = agent_loop._tool_dispatch_context(context, "call-2")
        other_root = agent_loop._tool_dispatch_context(
            self.context(run_id="root-b", root_run_id="root-b"),
            "call-1",
        )
        uuid.UUID(str(first.tool_operation_id))
        self.assertEqual(first.tool_operation_id, retry.tool_operation_id)
        self.assertNotEqual(first.tool_operation_id, other_call.tool_operation_id)
        self.assertNotEqual(first.tool_operation_id, other_root.tool_operation_id)
        self.assertEqual(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            context.tool_operation_id,
        )


class SkillProcessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_root_agent_finally_cleans_root_processes(self) -> None:
        async def fake_stream(
            *,
            user_id: str = "default",
            session_id: str = "default",
            run_id: str | None = None,
            root_run_id: str | None = None,
            parent_run_id: str | None = None,
            agent_kind: str = "primary",
            depth: int = 0,
            _browser_run_scope_id: str | None = None,
        ):
            del (
                user_id,
                session_id,
                run_id,
                root_run_id,
                parent_run_id,
                agent_kind,
                depth,
                _browser_run_scope_id,
            )
            yield {"type": "done", "finish_reason": "stop"}

        wrapped = agent_loop._emit_run_cancelled_on_cancellation(fake_stream)
        root_cleanup = AsyncMock(return_value={"success": True})
        browser_cleanup = AsyncMock()
        with (
            patch(
                "tools.skill_process.cleanup_skill_process_root",
                root_cleanup,
            ),
            patch("tools.browser.close_browser_run", browser_cleanup),
        ):
            child_events = [
                event
                async for event in wrapped(
                    user_id="u",
                    session_id="s",
                    run_id="child",
                    root_run_id="root",
                    parent_run_id="root",
                    agent_kind="delegate",
                    depth=1,
                )
            ]
            root_events = [
                event
                async for event in wrapped(
                    user_id="u",
                    session_id="s",
                    run_id="root",
                    root_run_id="root",
                    parent_run_id=None,
                    agent_kind="primary",
                    depth=0,
                )
            ]
        self.assertEqual("done", child_events[0]["type"])
        self.assertEqual("done", root_events[0]["type"])
        root_cleanup.assert_awaited_once_with("u", "s", "root")
        self.assertEqual(2, browser_cleanup.await_count)

    async def test_session_cleanup_merges_mcp_and_process_results(self) -> None:
        execution_cleanup = AsyncMock(return_value={
            "success": True,
            "registered_count": 0,
        })
        mcp_cleanup = AsyncMock(return_value={
            "success": True,
            "removed_config": True,
        })
        process_cleanup = AsyncMock(return_value={
            "success": True,
            "matched": 2,
            "closed": 2,
            "failures": [],
        })
        python_cleanup = unittest.mock.Mock(return_value=True)
        with (
            patch(
                "tools.session_execution_registry.revoke_session_executions",
                execution_cleanup,
            ),
            patch(
                "tools.mcp_client.cleanup_session_runtime",
                mcp_cleanup,
            ),
            patch(
                "tools.skill_process.cleanup_skill_process_session",
                process_cleanup,
            ),
            patch(
                "runtime.python_env.clean_session_runtime",
                python_cleanup,
            ),
        ):
            result = await harness_main.internal_session_cleanup("u", "s")
        self.assertTrue(result["success"])
        self.assertTrue(result["removed_config"])
        self.assertEqual(2, result["skill_processes"]["closed"])
        self.assertTrue(result["python_runtime"]["removed"])
        self.assertTrue(result["execution_revocation"]["success"])
        mcp_cleanup.assert_awaited_once_with("u", "s")
        process_cleanup.assert_awaited_once_with("u", "s")
        python_cleanup.assert_called_once_with("u", "s")

    async def test_session_cleanup_is_single_flight_and_cancellation_drained(
        self,
    ) -> None:
        process_started = asyncio.Event()
        release_process = asyncio.Event()

        async def slow_process_cleanup(_user_id, _session_id):
            process_started.set()
            await release_process.wait()
            return {
                "success": True,
                "matched": 1,
                "closed": 1,
                "failures": [],
            }

        execution_cleanup = AsyncMock(return_value={"success": True})
        process_cleanup = AsyncMock(side_effect=slow_process_cleanup)
        mcp_cleanup = AsyncMock(return_value={"success": True})
        python_cleanup = unittest.mock.Mock(return_value=False)
        with (
            patch(
                "tools.session_execution_registry.revoke_session_executions",
                execution_cleanup,
            ),
            patch(
                "tools.skill_process.cleanup_skill_process_session",
                process_cleanup,
            ),
            patch(
                "tools.mcp_client.cleanup_session_runtime",
                mcp_cleanup,
            ),
            patch(
                "runtime.python_env.clean_session_runtime",
                python_cleanup,
            ),
        ):
            first = asyncio.create_task(
                harness_main.internal_session_cleanup("u-flight", "s-flight")
            )
            second = asyncio.create_task(
                harness_main.internal_session_cleanup("u-flight", "s-flight")
            )
            await process_started.wait()
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            release_process.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second_result = await second

        self.assertTrue(second_result["success"])
        self.assertEqual(1, process_cleanup.await_count)
        self.assertEqual(1, execution_cleanup.await_count)
        self.assertEqual(1, mcp_cleanup.await_count)
        self.assertEqual(1, python_cleanup.call_count)
        self.assertNotIn(
            ("u-flight", "s-flight"),
            harness_main._SESSION_CLEANUP_FLIGHTS,
        )

    async def test_session_cleanup_failure_receipt_fails_closed(self) -> None:
        with (
            patch(
                "tools.session_execution_registry.revoke_session_executions",
                new=AsyncMock(return_value={"success": False}),
            ),
            patch(
                "tools.skill_process.cleanup_skill_process_session",
                new=AsyncMock(return_value={"success": True}),
            ),
            patch(
                "tools.mcp_client.cleanup_session_runtime",
                new=AsyncMock(return_value={"success": True}),
            ),
            patch(
                "runtime.python_env.clean_session_runtime",
                new=unittest.mock.Mock(return_value=True),
            ),
        ):
            result = await harness_main.internal_session_cleanup(
                "u-fail",
                "s-fail",
            )
        self.assertFalse(result["success"])
        self.assertFalse(result["execution_revocation"]["success"])

    async def test_shutdown_cleanup_entrypoint_is_callable(self) -> None:
        close_all = AsyncMock(return_value={"success": True, "closed": 0})
        with patch.object(
            skill_process,
            "_MANAGER",
            unittest.mock.Mock(close_all=close_all),
        ):
            result = await skill_process.close_all_skill_processes()
        self.assertTrue(result["success"])
        close_all.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
