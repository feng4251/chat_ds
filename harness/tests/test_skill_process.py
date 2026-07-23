from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import agent_loop
import main as harness_main
from tools.context import ToolContext
from tools.isolated_skill_executor import (
    IsolatedProcessLease,
    IsolatedSkillExecutorError,
    compute_skill_package_digest,
    create_process_owner_scope,
)
from tools.registry import get_metadata, get_schemas, preflight
from tools import skill_process


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
    ) -> tuple[dict, AsyncMock, AsyncMock]:
        context = context or self.context()
        lease = self.fake_lease(
            context=context,
            invocation_mode=invocation_mode,
            class_name=class_name,
            factory_name=factory_name,
        )
        opened = AsyncMock(return_value=(
            lease,
            {
                "status": "success",
                "state": "open",
                "runtime_profile": runtime_profile,
                "network_policy": {
                    "direct": "disabled",
                    "egress": (
                        "policy_proxy"
                        if runtime_profile == "browser-automation-v1"
                        else "none"
                    ),
                },
            },
        ))
        started = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": runtime_profile,
            "network_policy": {
                "direct": "disabled",
                "egress": (
                    "policy_proxy"
                    if runtime_profile == "browser-automation-v1"
                    else "none"
                ),
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

    async def test_browser_import_requires_dedicated_socket_without_fallback(self) -> None:
        self.script.write_text(
            'const { chromium } = require("playwright");\n',
            encoding="utf-8",
        )
        context = self.context()
        opened = AsyncMock()
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
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path="skills/portable-process/scripts/session.cjs",
                context=context,
            ))
        self.assertEqual("browser_runtime_unavailable", result["error_code"])
        opened.assert_not_awaited()

        lease = self.fake_lease(
            context=context,
            socket_path="/browser.sock",
        )
        opened = AsyncMock(return_value=(lease, {
            "status": "success",
            "state": "open",
            "runtime_profile": "browser-automation-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "policy_proxy",
            },
        }))
        started = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "browser-automation-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "policy_proxy",
            },
        })
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
        self.assertEqual("success", result["status"])
        self.assertEqual(
            "/browser.sock",
            opened.await_args.kwargs["socket_path"],
        )

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
        opened = AsyncMock()
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
        ):
            result = json.loads(await skill_process.run_skill_process(
                operation="start",
                script_path="skills/portable-process/scripts/session.cjs",
                context=context,
            ))
        self.assertEqual("browser_runtime_unavailable", result["error_code"])
        opened.assert_not_awaited()

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
                "runtime_profile": "base-v1",
            }

        close = AsyncMock()
        started = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "base-v1",
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
                "runtime_profile": "base-v1",
            }

        close = AsyncMock()
        started = AsyncMock(return_value={
            "status": "success",
            "state": "running",
            "runtime_profile": "base-v1",
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
                "call_id": "00000000-0000-0000-0000-000000000001",
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
                "00000000-0000-0000-0000-000000000001",
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
                "runtime_profile": "base-v1",
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
                    "runtime_profile": "base-v1",
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
            "runtime_profile": "base-v1",
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
        with (
            patch(
                "tools.mcp_client.cleanup_session_runtime",
                mcp_cleanup,
            ),
            patch(
                "tools.skill_process.cleanup_skill_process_session",
                process_cleanup,
            ),
        ):
            result = await harness_main.internal_session_cleanup("u", "s")
        self.assertTrue(result["success"])
        self.assertTrue(result["removed_config"])
        self.assertEqual(2, result["skill_processes"]["closed"])
        mcp_cleanup.assert_awaited_once_with("u", "s")
        process_cleanup.assert_awaited_once_with("u", "s")

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
