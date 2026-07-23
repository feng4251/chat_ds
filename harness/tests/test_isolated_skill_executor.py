from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from executor import server
from tools import isolated_skill_executor as client


V2_AUTH_TOKEN = "test-only-v2-auth-token-" + "x" * 32


def _artifact(path: str, content: bytes, *, change: str = "created") -> dict[str, object]:
    return {
        "path": path,
        "change": change,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


class IsolatedSkillExecutorClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.skill = self.root / "skill"
        self.workspace = self.root / "workspace"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "assets").mkdir()
        self.workspace.mkdir()
        (self.skill / "SKILL.md").write_text(
            "---\nname: portable-fixture\n---\n", encoding="utf-8"
        )
        (self.skill / "scripts" / "task.sh").write_bytes(b"printf portable\n")
        (self.skill / "assets" / "binary.bin").write_bytes(b"\x00\xffasset")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_runtime_capability_response_requires_exact_identity_and_never_accepts_env_values(self) -> None:
        request, encoded = client.build_runtime_capabilities_request(
            requirements=["packaging>=20"],
            commands=["python"],
            environment_variables=["CHATDS_WORKSPACE"],
            platform_groups=[["linux"]],
        )
        self.assertLess(len(encoded), client.MAX_REQUEST_BYTES)
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "runtime_capabilities_result",
            "request_id": request["request_id"],
            "status": "success",
            "valid": True,
            "runtime_identity": {
                "execution_runtime": "isolated_skill_executor",
                "python_implementation": "cpython",
                "python_version": "3.12.1",
                "platform": "linux",
                "network": "disabled",
                "dependency_install": "disabled",
            },
            "requirements": [{
                "requirement": "packaging>=20",
                "status": "satisfied",
                "satisfied": True,
                "installed_version": "25.0",
            }],
            "commands": [{"name": "python", "available": True}],
            "environment_variables": [{
                "name": "CHATDS_WORKSPACE",
                "available": True,
            }],
            "platform_groups": [{
                "allowed": ["linux"],
                "current": "linux",
                "satisfied": True,
            }],
        }
        validated = client.validate_runtime_capabilities_response(
            response,
            request=request,
        )
        self.assertTrue(validated["valid"])

        response["environment_variables"][0]["value"] = "must-not-cross-boundary"
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.validate_runtime_capabilities_response(response, request=request)
        self.assertEqual("invalid_response", caught.exception.code)

        response["environment_variables"][0].pop("value")
        response["request_id"] = str(uuid.uuid4())
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.validate_runtime_capabilities_response(response, request=request)
        self.assertEqual("invalid_response", caught.exception.code)

    def test_request_contains_exact_binary_skill_and_bounded_workspace_snapshot(self) -> None:
        (self.workspace / "input.bin").write_bytes(b"\x00input")
        for skipped in (".chatds", "debug", "cache"):
            directory = self.workspace / skipped
            directory.mkdir()
            (directory / "ignored.txt").write_text("ignored", encoding="utf-8")

        payload, encoded = client.build_skill_script_request(
            skill_root=self.skill,
            workspace=self.workspace,
            entrypoint="scripts/task.sh",
            args=["$(touch never)"],
            timeout=20,
            cwd="script",
            request_id=str(uuid.uuid4()),
        )

        self.assertLessEqual(len(encoded), client.MAX_REQUEST_BYTES)
        self.assertEqual("skill_script", payload["kind"])
        skill_files = {item["path"]: item for item in payload["skill_files"]}
        self.assertEqual(
            {"SKILL.md", "assets/binary.bin", "scripts/task.sh"}, set(skill_files)
        )
        self.assertEqual(
            b"\x00\xffasset",
            base64.b64decode(skill_files["assets/binary.bin"]["content_b64"]),
        )
        self.assertEqual(["input.bin"], [item["path"] for item in payload["workspace_files"]])

    def test_expected_skill_digest_rejects_snapshot_mutation_before_exchange(self) -> None:
        expected = client.compute_skill_package_digest(self.skill)
        (self.skill / "assets" / "binary.bin").write_bytes(b"mutated-after-authority")
        exchange = AsyncMock()
        with patch.object(client.asyncio, "open_unix_connection", exchange):
            with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
                asyncio.run(client.execute_isolated_skill_script(
                    skill_root=self.skill,
                    workspace=self.workspace,
                    entrypoint="scripts/task.sh",
                    expected_skill_sha256=expected,
                ))
        self.assertEqual("authority_digest_mismatch", caught.exception.code)
        exchange.assert_not_awaited()

    def test_pending_sync_retries_partial_batch_and_reuses_fixed_ack_op_id(self) -> None:
        scope = client.create_process_owner_scope(
            user_id="sync-user",
            session_id="sync-session",
            root_run_id="sync-run",
        )
        lease = client.IsolatedProcessLease(
            handle="pl2_" + "a" * 32 + "_" + "b" * 32,
            skill_sha256="c" * 64,
            script_sha256="d" * 64,
            entrypoint="scripts/task.sh",
            invocation_mode="cli",
            class_name=None,
            factory_name=None,
            _owner_scope=scope,
            _workspace=self.workspace,
            _socket_path="/unused",
            _baseline={},
        )
        artifacts = [
            ("one.txt", b"one", _artifact("one.txt", b"one")),
            ("two.txt", b"two", _artifact("two.txt", b"two")),
        ]
        prepared = {
            "sync_token": "s" * 43,
            "sync_pending": True,
            "state": "running",
        }
        acknowledged = {
            "state": "running",
            "acknowledged_operation": "sync",
            "sync_acknowledged": True,
        }
        execute = AsyncMock(side_effect=[
            (prepared, artifacts),
            (acknowledged, []),
        ])
        real_replace = os.replace
        replace_calls = 0

        def fail_second_replace(source: object, target: object) -> None:
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("injected second replace failure")
            real_replace(source, target)

        with patch.object(client, "_execute_process_operation", execute), patch.object(
            client.os,
            "replace",
            side_effect=fail_second_replace,
        ):
            with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
                asyncio.run(client.sync_isolated_process_artifacts(lease))
        self.assertEqual("artifact_apply_failed", caught.exception.code)
        self.assertEqual(b"one", (self.workspace / "one.txt").read_bytes())
        self.assertFalse((self.workspace / "two.txt").exists())
        self.assertEqual(1, execute.await_count)
        fixed_ack_op_id = lease._pending_sync_ack_op_id

        with patch.object(client, "_execute_process_operation", execute):
            result = asyncio.run(client.sync_isolated_process_artifacts(lease))

        self.assertEqual(b"one", (self.workspace / "one.txt").read_bytes())
        self.assertEqual(b"two", (self.workspace / "two.txt").read_bytes())
        self.assertTrue(result["sync_acknowledged"])
        self.assertFalse(result["sync_pending"])
        self.assertEqual(2, execute.await_count)
        self.assertEqual("ack", execute.await_args_list[1].args[1])
        self.assertEqual(
            fixed_ack_op_id,
            execute.await_args_list[1].kwargs["op_id"],
        )

    def test_lost_sync_response_is_recovered_with_same_prepare_op_id(self) -> None:
        scope = client.create_process_owner_scope(
            user_id="uncertain-user",
            session_id="uncertain-session",
            root_run_id="uncertain-run",
        )
        lease = client.IsolatedProcessLease(
            handle="pl2_" + "3" * 32 + "_" + "4" * 32,
            skill_sha256="5" * 64,
            script_sha256="6" * 64,
            entrypoint="scripts/task.sh",
            invocation_mode="cli",
            class_name=None,
            factory_name=None,
            _owner_scope=scope,
            _workspace=self.workspace,
            _socket_path="/unused",
            _baseline={},
        )
        prepared = {
            "sync_token": "u" * 43,
            "sync_pending": True,
            "state": "running",
        }
        acknowledged = {
            "state": "running",
            "acknowledged_operation": "sync",
            "sync_acknowledged": True,
        }
        execute = AsyncMock(side_effect=[
            client.IsolatedSkillExecutorError(
                "executor_unavailable",
                "response lost after server dispatch",
            ),
            (prepared, []),
            (acknowledged, []),
        ])
        with patch.object(client, "_execute_process_operation", execute):
            with self.assertRaises(client.IsolatedSkillExecutorError):
                asyncio.run(client.sync_isolated_process_artifacts(lease))
            retained_prepare_op_id = lease._pending_sync_prepare_op_id
            result = asyncio.run(client.sync_isolated_process_artifacts(lease))

        self.assertTrue(result["sync_acknowledged"])
        self.assertEqual(3, execute.await_count)
        self.assertEqual(
            retained_prepare_op_id,
            execute.await_args_list[0].kwargs["op_id"],
        )
        self.assertEqual(
            retained_prepare_op_id,
            execute.await_args_list[1].kwargs["op_id"],
        )
        self.assertEqual("ack", execute.await_args_list[2].args[1])

    def test_cli_stdin_close_api_and_sixty_second_read_wait_bound(self) -> None:
        scope = client.create_process_owner_scope(
            user_id="eof-user",
            session_id="eof-session",
            root_run_id="eof-run",
        )
        lease = client.IsolatedProcessLease(
            handle="pl2_" + "7" * 32 + "_" + "8" * 32,
            skill_sha256="9" * 64,
            script_sha256="a" * 64,
            entrypoint="scripts/task.sh",
            invocation_mode="cli",
            class_name=None,
            factory_name=None,
            _owner_scope=scope,
            _workspace=self.workspace,
            _socket_path="/unused",
            _baseline={},
        )
        execute = AsyncMock(return_value=({
            "stdin_closed": True,
            "already_closed": False,
            "state": "running",
        }, []))
        with patch.object(client, "_execute_process_operation", execute):
            receipt = asyncio.run(client.close_isolated_process_stdin(lease))
        self.assertTrue(receipt["stdin_closed"])
        self.assertEqual("stdin_close", execute.await_args.args[1])

        read_response = {
            "stdout_b64": "",
            "stderr_b64": "",
            "state": "running",
        }
        execute = AsyncMock(return_value=(read_response, []))
        with patch.object(client, "_execute_process_operation", execute):
            result = asyncio.run(client.read_isolated_process_output(
                lease,
                wait_ms=60_000,
            ))
        self.assertEqual(b"", result["stdout_bytes"])
        self.assertEqual(70, execute.await_args.kwargs["timeout"])

    def test_pending_close_cas_failure_retries_same_batch_before_closed(self) -> None:
        existing = self.workspace / "existing.txt"
        existing.write_bytes(b"before")
        before_identity = (
            len(b"before"),
            hashlib.sha256(b"before").hexdigest(),
        )
        scope = client.create_process_owner_scope(
            user_id="close-user",
            session_id="close-session",
            root_run_id="close-run",
        )
        lease = client.IsolatedProcessLease(
            handle="pl2_" + "e" * 32 + "_" + "f" * 32,
            skill_sha256="1" * 64,
            script_sha256="2" * 64,
            entrypoint="scripts/task.sh",
            invocation_mode="cli",
            class_name=None,
            factory_name=None,
            _owner_scope=scope,
            _workspace=self.workspace,
            _socket_path="/unused",
            _baseline={"existing.txt": before_identity},
        )
        artifacts = [(
            "existing.txt",
            b"after",
            _artifact("existing.txt", b"after", change="modified"),
        )]
        prepared = {
            "sync_token": "t" * 43,
            "sync_pending": True,
            "state": "closing",
        }
        acknowledged = {
            "state": "closed",
            "acknowledged_operation": "close",
            "sync_acknowledged": True,
        }
        execute = AsyncMock(side_effect=[
            (prepared, artifacts),
            (acknowledged, []),
        ])
        existing.write_bytes(b"concurrent")
        with patch.object(client, "_execute_process_operation", execute):
            with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
                asyncio.run(client.close_isolated_process_lease(lease))
        self.assertEqual("workspace_concurrent_modification", caught.exception.code)
        self.assertFalse(lease.closed)
        self.assertEqual(1, execute.await_count)
        fixed_ack_op_id = lease._pending_sync_ack_op_id

        existing.write_bytes(b"before")
        with patch.object(client, "_execute_process_operation", execute):
            result = asyncio.run(client.close_isolated_process_lease(lease))
        self.assertEqual(b"after", existing.read_bytes())
        self.assertTrue(lease.closed)
        self.assertEqual("closed", result["state"])
        self.assertEqual(
            fixed_ack_op_id,
            execute.await_args_list[1].kwargs["op_id"],
        )

    def test_v2_client_server_commonjs_authority_and_operation_round_trip(self) -> None:
        script = self.skill / "scripts" / "session.cjs"
        script.write_text(
            '"use strict";\n'
            'const readline = require("readline");\n'
            'const input = readline.createInterface({input: process.stdin});\n'
            'input.on("line", line => console.log(line));\n',
            encoding="utf-8",
        )
        scope = client.create_process_owner_scope(
            user_id="user-client",
            session_id="session-client",
            root_run_id="run-client",
        )
        with patch.dict(
            os.environ,
            {
                "EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN,
                "EXECUTOR_ALLOWED_REQUEST_KINDS": "runtime_capabilities,process_lease",
                "EXECUTOR_WORKER_UID": str(
                    65532 if os.geteuid() != 65532 else 65531
                ),
                "EXECUTOR_WORKER_GID": str(
                    65532 if os.getegid() != 65532 else 65531
                ),
            },
        ):
            server._shutdown_all_process_leases()
            try:
                request, encoded = client.build_process_lease_open_request(
                    owner_scope=scope,
                    skill_root=self.skill,
                    workspace=self.workspace,
                    entrypoint="scripts/session.cjs",
                    idle_ttl_seconds=30,
                    max_runtime_seconds=60,
                )
                self.assertEqual(request, json.loads(encoded))
                self.assertNotIn(V2_AUTH_TOKEN, encoded.decode("utf-8"))
                with patch.object(server, "_prepare_worker_tree", return_value=None):
                    opened = server._run(request)
                validated, artifacts = client.validate_process_lease_response(
                    opened,
                    request=request,
                )
                self.assertEqual([], artifacts)
                self.assertEqual("node", validated["interpreter"])
                lease = client.IsolatedProcessLease(
                    handle=validated["lease_handle"],
                    skill_sha256=request["skill_sha256"],
                    script_sha256=request["script_sha256"],
                    entrypoint=request["entrypoint"],
                    invocation_mode="cli",
                    class_name=None,
                    factory_name=None,
                    _owner_scope=scope,
                    _workspace=self.workspace,
                    _socket_path="/unused-in-direct-roundtrip",
                    _baseline={
                        item["path"]: (item["size_bytes"], item["sha256"])
                        for item in request["workspace_files"]
                    },
                )

                start_request, _ = client._build_process_operation_request(
                    lease,
                    "start",
                )
                with patch.object(
                    server,
                    "_native_worker_popen_kwargs",
                    return_value={"start_new_session": True},
                ), patch.object(
                    server,
                    "_resource_limited_command",
                    side_effect=lambda command, **_: command,
                ):
                    started = server._run(start_request)
                client.validate_process_lease_response(
                    started,
                    request=start_request,
                )

                write_request, _ = client._build_process_operation_request(
                    lease,
                    "write",
                    extra={
                        "stdin_b64": base64.b64encode(b'{"value":"client"}\n').decode("ascii")
                    },
                )
                written = server._run(write_request)
                client.validate_process_lease_response(
                    written,
                    request=write_request,
                )

                read_request, _ = client._build_process_operation_request(
                    lease,
                    "read",
                    extra={
                        "stdout_offset": 0,
                        "stderr_offset": 0,
                        "max_bytes": 4096,
                        "wait_ms": 1_000,
                    },
                )
                read = server._run(read_request)
                client.validate_process_lease_response(read, request=read_request)
                self.assertEqual(
                    b'{"value":"client"}\n',
                    base64.b64decode(read["stdout_b64"]),
                )

                stdin_close_request, _ = client._build_process_operation_request(
                    lease,
                    "stdin_close",
                )
                stdin_closed = server._run(stdin_close_request)
                client.validate_process_lease_response(
                    stdin_closed,
                    request=stdin_close_request,
                )
                self.assertIs(stdin_closed["stdin_closed"], True)

                close_request, _ = client._build_process_operation_request(
                    lease,
                    "close",
                )
                with patch.object(
                    server,
                    "_sweep_configured_worker_uid",
                    return_value=None,
                ):
                    prepared_close = server._run(close_request)
                    client.validate_process_lease_response(
                        prepared_close,
                        request=close_request,
                    )
                    ack_request, _ = client._build_process_operation_request(
                        lease,
                        "ack",
                        extra={"sync_token": prepared_close["sync_token"]},
                    )
                    closed = server._run(ack_request)
                client.validate_process_lease_response(
                    closed,
                    request=ack_request,
                )
                self.assertEqual("closed", closed["state"])
            finally:
                server._shutdown_all_process_leases()

    def test_v2_owner_scope_is_not_a_free_form_model_argument(self) -> None:
        with patch.dict(
            os.environ,
            {"EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN},
        ):
            with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
                client.build_process_lease_open_request(
                    owner_scope={  # type: ignore[arg-type]
                        "user_id": "forged",
                        "session_id": "forged",
                        "root_run_id": "forged",
                    },
                    skill_root=self.skill,
                    workspace=self.workspace,
                    entrypoint="scripts/task.sh",
                )
        self.assertEqual("invalid_owner_scope", caught.exception.code)

    def test_startup_reap_request_and_receipt_have_no_model_owner_capability(self) -> None:
        with patch.dict(
            os.environ,
            {
                "EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN,
                "EXECUTOR_ALLOWED_REQUEST_KINDS": (
                    "runtime_capabilities,process_lease"
                ),
                "EXECUTOR_RUNTIME_PROFILE": "base-v1",
                "EXECUTOR_WORKER_UID": str(
                    65532 if os.geteuid() != 65532 else 65531
                ),
                "EXECUTOR_WORKER_GID": str(
                    65532 if os.getegid() != 65532 else 65531
                ),
            },
        ), patch.object(
            server,
            "_sweep_configured_worker_uid",
            return_value=None,
        ):
            request, encoded = client.build_process_reap_request()
            self.assertEqual(request, json.loads(encoded))
            self.assertNotIn("owner_scope", request)
            self.assertNotIn("lease_handle", request)
            self.assertNotIn(V2_AUTH_TOKEN, encoded.decode("utf-8"))

            response = server._run(request)
            validated, artifacts = client.validate_process_lease_response(
                response,
                request=request,
            )

        self.assertEqual([], artifacts)
        self.assertEqual("success", validated["status"])
        self.assertEqual(0, validated["reaped_leases"])
        self.assertIs(validated["worker_processes_empty"], True)

    def test_v2_factory_invocation_is_exact_and_mutually_exclusive_with_class(self) -> None:
        script = self.skill / "scripts" / "factory.py"
        script.write_text(
            "class Session:\n"
            "    def step(self, value):\n"
            "        return value\n\n"
            "def open_session(prefix=''):\n"
            "    return Session()\n",
            encoding="utf-8",
        )
        scope = client.create_process_owner_scope(
            user_id="factory-user",
            session_id="factory-session",
            root_run_id="factory-run",
        )
        with patch.dict(
            os.environ,
            {"EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN},
        ):
            request, _ = client.build_process_lease_open_request(
                owner_scope=scope,
                skill_root=self.skill,
                workspace=self.workspace,
                entrypoint="scripts/factory.py",
                factory_name="open_session",
                constructor_args=["prefix:"],
                constructor_kwargs={},
            )
            self.assertEqual({
                "mode": "factory",
                "factory_name": "open_session",
                "factory_args": ["prefix:"],
                "factory_kwargs": {},
            }, request["invocation"])

            with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
                client.build_process_lease_open_request(
                    owner_scope=scope,
                    skill_root=self.skill,
                    workspace=self.workspace,
                    entrypoint="scripts/factory.py",
                    class_name="Session",
                    factory_name="open_session",
                )
        self.assertEqual("invalid_function_call", caught.exception.code)

    def test_v2_transport_retry_reuses_the_exact_signed_request_and_op_id(self) -> None:
        scope = client.create_process_owner_scope(
            user_id="retry-user",
            session_id="retry-session",
            root_run_id="retry-run",
        )
        with patch.dict(
            os.environ,
            {"EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN},
        ):
            request, encoded = client.build_process_lease_open_request(
                owner_scope=scope,
                skill_root=self.skill,
                workspace=self.workspace,
                entrypoint="scripts/task.sh",
            )
        response = {
            "protocol_version": client.PROCESS_PROTOCOL_VERSION,
            "kind": "process_lease_result",
            "operation": "open",
            "request_id": request["request_id"],
            "status": "success",
            "lease_handle": "pl2_" + "a" * 32 + "_" + "b" * 32,
            "scope_digest": "c" * 64,
            "skill_sha256": request["skill_sha256"],
            "script_sha256": request["script_sha256"],
            "state": "open",
            "network": "disabled",
            "runtime_profile": "base-v1",
            "network_policy": {"direct": "disabled", "egress": "none"},
        }

        class FakeReader:
            def __init__(self, raw: bytes):
                self.raw = raw

            async def readline(self) -> bytes:
                return self.raw

        class FakeWriter:
            def __init__(self):
                self.writes: list[bytes] = []

            def write(self, value: bytes) -> None:
                self.writes.append(value)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        first_writer = FakeWriter()
        second_writer = FakeWriter()
        connections = iter([
            (FakeReader(b""), first_writer),
            (
                FakeReader(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"),
                second_writer,
            ),
        ])

        async def fake_open(*args: object, **kwargs: object) -> tuple[FakeReader, FakeWriter]:
            return next(connections)

        with patch.object(
            client.asyncio,
            "open_unix_connection",
            side_effect=fake_open,
        ):
            validated, artifacts = asyncio.run(client._exchange_process_request(
                request,
                encoded,
                socket_path="/unused",
                timeout=1,
            ))

        self.assertEqual([], artifacts)
        self.assertEqual("success", validated["status"])
        self.assertEqual([encoded], first_writer.writes)
        self.assertEqual([encoded], second_writer.writes)
        self.assertEqual(request["op_id"], json.loads(first_writer.writes[0])["op_id"])
        self.assertEqual(request["op_id"], json.loads(second_writer.writes[0])["op_id"])

    def test_declared_command_request_and_receipt_have_exact_no_shell_identity(self) -> None:
        request, encoded = client.build_declared_command_request(
            skill_root=self.skill,
            workspace=self.workspace,
            executable="python",
            argv=["literal; $(touch never)"],
            cwd="skill",
            request_id=str(uuid.uuid4()),
        )
        self.assertLess(len(encoded), client.MAX_REQUEST_BYTES)
        self.assertEqual("declared_command", request["kind"])
        self.assertNotIn("command", request)
        self.assertNotIn("shell", request)
        invocation = json.dumps(
            ["python", "literal; $(touch never)"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "declared_command_result",
            "request_id": request["request_id"],
            "status": "success",
            "executable": "python",
            "cwd": "skill",
            "network": "disabled",
            "shell": False,
            "command_sha256": hashlib.sha256(invocation).hexdigest(),
            "stdout": "literal; $(touch never)\n",
            "stderr": "",
            "artifacts": [_artifact("result.txt", b"ok")],
        }
        decoded = client.validate_declared_command_response(
            response, request=request
        )
        receipts = client.apply_artifacts_atomically(
            self.workspace, decoded, baseline={}
        )
        self.assertEqual("result.txt", receipts[0]["path"])

        response["shell"] = True
        with self.assertRaises(client.IsolatedSkillExecutorError):
            client.validate_declared_command_response(response, request=request)

        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.build_declared_command_request(
                skill_root=self.skill,
                workspace=self.workspace,
                executable="/bin/sh",
            )
        self.assertEqual("invalid_executable", caught.exception.code)

    def test_function_invocation_is_declarative_bounded_and_identity_checked(self) -> None:
        script = self.skill / "scripts" / "helper.py"
        script.write_text("def search(query, limit=2):\n    return [query] * limit\n", encoding="utf-8")
        request_id = str(uuid.uuid4())
        payload, _ = client.build_skill_script_request(
            skill_root=self.skill,
            workspace=self.workspace,
            entrypoint="scripts/helper.py",
            function_name="search",
            function_args=["target"],
            function_kwargs={"limit": 3},
            request_id=request_id,
        )

        self.assertEqual([], payload["argv"])
        self.assertEqual({
            "mode": "function",
            "name": "search",
            "args": ["target"],
            "kwargs": {"limit": 3},
        }, payload["invocation"])

        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "skill_script_result",
            "request_id": request_id,
            "status": "success",
            "invocation_mode": "function",
            "function_name": "search",
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "result": ["target", "target", "target"],
            "artifacts": [],
        }
        self.assertEqual([], client.validate_skill_script_response(
            response,
            request_id=request_id,
            invocation_mode="function",
            function_name="search",
        ))
        response["function_name"] = "different"
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.validate_skill_script_response(
                response,
                request_id=request_id,
                invocation_mode="function",
                function_name="search",
            )
        self.assertEqual("invalid_response", caught.exception.code)

    def test_function_invocation_rejects_code_like_names_and_invalid_json(self) -> None:
        script = self.skill / "scripts" / "helper.py"
        script.write_text("def public(value=None):\n    return value\n", encoding="utf-8")
        invalid_calls = [
            {"function_name": "os.system"},
            {"function_name": "_private"},
            {"function_name": "public", "args": ["cli"]},
            {"function_name": "public", "function_args": [math.nan]},
        ]
        for invocation in invalid_calls:
            with self.subTest(invocation=invocation):
                with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
                    client.build_skill_script_request(
                        skill_root=self.skill,
                        workspace=self.workspace,
                        entrypoint="scripts/helper.py",
                        **invocation,
                    )
                self.assertEqual("invalid_function_call", caught.exception.code)

        nested: object = "leaf"
        for _ in range(client.MAX_FUNCTION_JSON_DEPTH + 2):
            nested = [nested]
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.build_skill_script_request(
                skill_root=self.skill,
                workspace=self.workspace,
                entrypoint="scripts/helper.py",
                function_name="public",
                function_args=[nested],
            )
        self.assertEqual("invalid_function_call", caught.exception.code)

    def test_validated_artifacts_are_applied_with_atomic_file_replacement(self) -> None:
        (self.workspace / "existing.txt").write_bytes(b"before")
        request_id = str(uuid.uuid4())
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "skill_script_result",
            "request_id": request_id,
            "status": "success",
            "artifacts": [
                _artifact("existing.txt", b"after", change="modified"),
                _artifact("nested/result.bin", b"\x00\xffresult"),
            ],
        }

        decoded = client.validate_skill_script_response(response, request_id=request_id)
        receipts = client.apply_artifacts_atomically(self.workspace, decoded)

        self.assertEqual(b"after", (self.workspace / "existing.txt").read_bytes())
        self.assertEqual(b"\x00\xffresult", (self.workspace / "nested" / "result.bin").read_bytes())
        self.assertEqual(
            {"existing.txt", "nested/result.bin"}, {item["path"] for item in receipts}
        )
        self.assertFalse(list(self.workspace.rglob(".chatds-artifact-*")))

    def test_visual_artifact_larger_than_eight_mib_is_supported_within_batch_bound(self) -> None:
        content = b"\x89PNG\r\n\x1a\n" + b"x" * (9 * 1024 * 1024)
        request_id = str(uuid.uuid4())
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "skill_script_result",
            "request_id": request_id,
            "status": "success",
            "artifacts": [_artifact("screenshots/full-page.png", content)],
        }

        decoded = client.validate_skill_script_response(
            response,
            request_id=request_id,
        )
        receipts = client.apply_artifacts_atomically(
            self.workspace,
            decoded,
        )

        self.assertGreater(receipts[0]["size_bytes"], 8 * 1024 * 1024)
        self.assertEqual(
            hashlib.sha256(content).hexdigest(),
            receipts[0]["sha256"],
        )
        self.assertEqual(
            content,
            (self.workspace / "screenshots" / "full-page.png").read_bytes(),
        )

    def test_session_code_request_snapshots_workspace_and_optional_skills(self) -> None:
        (self.workspace / "input.bin").write_bytes(b"\x00input")
        request_id = str(uuid.uuid4())

        payload, encoded = client.build_session_code_request(
            workspace=self.workspace,
            skills_root=self.skill,
            code="from pathlib import Path\nprint(Path('input.bin').read_bytes())",
            timeout=20,
            request_id=request_id,
        )

        self.assertLessEqual(len(encoded), client.MAX_REQUEST_BYTES)
        self.assertEqual("session_code", payload["kind"])
        self.assertEqual(request_id, payload["request_id"])
        self.assertEqual(["input.bin"], [item["path"] for item in payload["workspace_files"]])
        self.assertEqual(
            {"SKILL.md", "assets/binary.bin", "scripts/task.sh"},
            {item["path"] for item in payload["skill_files"]},
        )

    def test_session_code_response_has_complete_verified_artifact_receipts(self) -> None:
        request_id = str(uuid.uuid4())
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "session_code_result",
            "request_id": request_id,
            "status": "success",
            "artifacts": [
                _artifact("one.txt", b"one"),
                _artifact("nested/two.bin", b"\x00two"),
            ],
        }

        decoded = client.validate_session_code_response(response, request_id=request_id)
        receipts = client.apply_artifacts_atomically(self.workspace, decoded)

        self.assertEqual(2, len(receipts))
        self.assertEqual(
            {"one.txt", "nested/two.bin"},
            {receipt["path"] for receipt in receipts},
        )
        self.assertTrue(all(len(receipt["sha256"]) == 64 for receipt in receipts))
        self.assertEqual(b"one", (self.workspace / "one.txt").read_bytes())
        self.assertEqual(b"\x00two", (self.workspace / "nested" / "two.bin").read_bytes())

    def test_atomic_apply_rejects_concurrent_workspace_changes_before_any_replace(self) -> None:
        existing = self.workspace / "existing.txt"
        existing.write_bytes(b"snapshot")
        baseline = {
            "existing.txt": (len(b"snapshot"), hashlib.sha256(b"snapshot").hexdigest()),
        }
        artifacts = [
            ("new.txt", b"new", _artifact("new.txt", b"new")),
            (
                "existing.txt",
                b"executor-result",
                _artifact("existing.txt", b"executor-result", change="modified"),
            ),
        ]
        existing.write_bytes(b"concurrent-worker")

        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.apply_artifacts_atomically(
                self.workspace,
                artifacts,
                baseline=baseline,
            )

        self.assertEqual("workspace_concurrent_modification", caught.exception.code)
        self.assertEqual(b"concurrent-worker", existing.read_bytes())
        self.assertFalse((self.workspace / "new.txt").exists())
        self.assertFalse(list(self.workspace.rglob(".chatds-artifact-*")))

    def test_atomic_apply_rejects_created_path_that_appeared_concurrently(self) -> None:
        target = self.workspace / "new.txt"
        target.write_bytes(b"concurrent-worker")

        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.apply_artifacts_atomically(
                self.workspace,
                [("new.txt", b"executor-result", _artifact("new.txt", b"executor-result"))],
                baseline={},
            )

        self.assertEqual("workspace_concurrent_modification", caught.exception.code)
        self.assertEqual(b"concurrent-worker", target.read_bytes())

    def test_apply_rechecks_target_after_staging_before_first_replace(self) -> None:
        target = self.workspace / "existing.txt"
        target.write_bytes(b"snapshot")
        baseline = {
            "existing.txt": (
                len(b"snapshot"),
                hashlib.sha256(b"snapshot").hexdigest(),
            ),
        }
        artifacts = [
            (
                "existing.txt",
                b"executor-result",
                _artifact(
                    "existing.txt",
                    b"executor-result",
                    change="modified",
                ),
            ),
        ]
        original_fsync = client.os.fsync
        mutated = False

        def mutate_after_stage(descriptor: int) -> None:
            nonlocal mutated
            original_fsync(descriptor)
            if not mutated:
                mutated = True
                # Simulate a non-cooperating writer that ignores the advisory
                # workspace lock while executor bytes are being staged.
                target.write_bytes(b"uncoordinated-writer")

        with patch.object(client.os, "fsync", side_effect=mutate_after_stage):
            with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
                client.apply_artifacts_atomically(
                    self.workspace,
                    artifacts,
                    baseline=baseline,
                )

        self.assertEqual(
            "workspace_concurrent_modification",
            caught.exception.code,
        )
        self.assertEqual(b"uncoordinated-writer", target.read_bytes())
        self.assertFalse(list(self.workspace.rglob(".chatds-artifact-*")))

    def test_session_code_request_rejects_unbounded_or_invalid_code(self) -> None:
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.build_session_code_request(workspace=self.workspace, code="")
        self.assertEqual("invalid_code", caught.exception.code)

        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.build_session_code_request(
                workspace=self.workspace,
                code="x" * (client.MAX_CODE_BYTES + 1),
            )
        self.assertEqual("code_limit_exceeded", caught.exception.code)

    def test_malicious_or_corrupt_response_is_rejected_before_workspace_write(self) -> None:
        request_id = str(uuid.uuid4())
        corrupt = _artifact("safe.txt", b"content")
        corrupt["sha256"] = "0" * 64
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "skill_script_result",
            "request_id": request_id,
            "status": "success",
            "artifacts": [corrupt],
        }
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.validate_skill_script_response(response, request_id=request_id)
        self.assertEqual("artifact_integrity_error", caught.exception.code)
        self.assertFalse((self.workspace / "safe.txt").exists())

        response["artifacts"] = [_artifact("../escape.txt", b"escape")]
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.validate_skill_script_response(response, request_id=request_id)
        self.assertEqual("invalid_path", caught.exception.code)
        self.assertFalse((self.root / "escape.txt").exists())

        response["artifacts"] = [_artifact(".chatds/control.json", b"pollution")]
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.validate_skill_script_response(response, request_id=request_id)
        self.assertEqual("unsafe_workspace_path", caught.exception.code)
        self.assertFalse((self.workspace / ".chatds" / "control.json").exists())

    def test_snapshot_and_apply_reject_links(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        linked = self.skill / "assets" / "linked.txt"
        try:
            linked.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.build_skill_script_request(
                skill_root=self.skill,
                workspace=self.workspace,
                entrypoint="scripts/task.sh",
            )
        self.assertEqual("unsafe_snapshot_file", caught.exception.code)

        linked.unlink()
        target = self.workspace / "target.txt"
        target.symlink_to(outside)
        metadata = _artifact("target.txt", b"replacement")
        decoded = [("target.txt", b"replacement", metadata)]
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.apply_artifacts_atomically(self.workspace, decoded)
        self.assertEqual("unsafe_workspace_path", caught.exception.code)
        self.assertEqual("outside", outside.read_text(encoding="utf-8"))

    def test_snapshot_rejects_hardlinked_files(self) -> None:
        source = self.root / "shared.bin"
        source.write_bytes(b"shared")
        linked = self.skill / "assets" / "hardlinked.bin"
        try:
            linked.hardlink_to(source)
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks unavailable")

        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.build_skill_script_request(
                skill_root=self.skill,
                workspace=self.workspace,
                entrypoint="scripts/task.sh",
            )
        self.assertEqual("unsafe_snapshot_file", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
