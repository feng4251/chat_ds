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
from tools.executor_slot_pool import (
    ExecutorSlotPoolError,
    executor_attestation_sha256,
)


V2_AUTH_TOKEN = "test-only-v2-auth-token-" + "x" * 32
TEST_RUNTIME_BUILD_SHA256 = hashlib.sha256(
    b"test-runtime-build"
).hexdigest()


def _retrieval_egress_rules(
    *origins: str,
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "methods": ["GET", "HEAD"],
            "url_prefix": f"{origin}/",
        }
        for origin in origins
    )


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

    def test_snapshot_tree_bounds_directory_entries_before_sorting(self) -> None:
        class Entry:
            def __init__(self, name: str) -> None:
                self.name = name

        class Scanner:
            def __init__(self) -> None:
                self.emitted = 0
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback) -> None:
                self.closed = True

            def __iter__(self):
                return self

            def __next__(self):
                if self.emitted > client.MAX_SNAPSHOT_ENTRIES:
                    raise AssertionError(
                        "snapshot walker consumed beyond remaining+1"
                    )
                self.emitted += 1
                return Entry(f"entry-{self.emitted:08d}")

        scanner = Scanner()
        with (
            patch.object(client.os, "scandir", return_value=scanner),
            patch.object(
                client.os,
                "listdir",
                side_effect=AssertionError(
                    "snapshot walker must not allocate os.listdir"
                ),
            ),
        ):
            with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
                client._snapshot_tree(
                    self.skill,
                    field="fixture",
                    max_files=client.MAX_SNAPSHOT_ENTRIES,
                    max_file_bytes=1,
                    max_total_bytes=1,
                )

        self.assertEqual("snapshot_limit_exceeded", caught.exception.code)
        self.assertEqual(client.MAX_SNAPSHOT_ENTRIES + 1, scanner.emitted)
        self.assertTrue(scanner.closed)

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
                "runtime_build_sha256": TEST_RUNTIME_BUILD_SHA256,
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
        heterogeneous = {
            **response,
            "runtime_identity": {
                **response["runtime_identity"],
                "runtime_build_sha256": hashlib.sha256(
                    b"heterogeneous-runtime-build"
                ).hexdigest(),
            },
        }
        client.validate_runtime_capabilities_response(
            heterogeneous,
            request=request,
        )
        self.assertNotEqual(
            executor_attestation_sha256(response),
            executor_attestation_sha256(heterogeneous),
        )

        response["environment_variables"][0]["value"] = "must-not-cross-boundary"
        with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
            client.validate_runtime_capabilities_response(response, request=request)
        self.assertEqual("invalid_response", caught.exception.code)

        response["environment_variables"][0].pop("value")
        for bad_digest in (
            None,
            "",
            "0" * 63,
            "0" * 65,
            "g" * 64,
            "A" * 64,
            int("1" * 64),
            False,
        ):
            if bad_digest is None:
                response["runtime_identity"].pop("runtime_build_sha256")
            else:
                response["runtime_identity"]["runtime_build_sha256"] = bad_digest
            with self.subTest(runtime_build_sha256=bad_digest):
                with self.assertRaises(
                    client.IsolatedSkillExecutorError
                ) as caught:
                    client.validate_runtime_capabilities_response(
                        response,
                        request=request,
                    )
                self.assertEqual("invalid_response", caught.exception.code)
            response["runtime_identity"][
                "runtime_build_sha256"
            ] = TEST_RUNTIME_BUILD_SHA256

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
        self.assertEqual([], payload["egress_origins"])
        self.assertEqual([], payload["egress_rules"])
        self.assertEqual([], payload["private_origins"])

    def test_runtime_owned_exact_egress_propagates_to_script_and_process_requests(
        self,
    ) -> None:
        origins = (
            "https://api.example.test:443",
            "http://catalog.example.test:80",
        )
        rules = (
            {
                "methods": ["GET", "HEAD", "POST"],
                "url_prefix": (
                    "https://api.example.test:443/v1/?tenant=alpha"
                ),
            },
            {
                "methods": ["GET", "HEAD"],
                "url_prefix": "http://catalog.example.test:80/lookup",
            },
        )
        script_request, script_encoded = client.build_skill_script_request(
            skill_root=self.skill,
            workspace=self.workspace,
            entrypoint="scripts/task.sh",
            egress_origins=origins,
            egress_rules=rules,
        )
        self.assertEqual(list(origins), script_request["egress_origins"])
        self.assertEqual(list(rules), script_request["egress_rules"])
        self.assertEqual(2, script_request["egress_policy_version"])
        self.assertEqual(
            list(origins),
            json.loads(script_encoded)["egress_origins"],
        )
        self.assertEqual(
            list(rules),
            json.loads(script_encoded)["egress_rules"],
        )

        scope = client.create_process_owner_scope(
            user_id="egress-user",
            session_id="egress-session",
            root_run_id="egress-run",
        )
        with patch.dict(
            os.environ,
            {"EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN},
        ):
            process_request, process_encoded = (
                client.build_process_lease_open_request(
                    owner_scope=scope,
                    skill_root=self.skill,
                    workspace=self.workspace,
                    entrypoint="scripts/task.sh",
                    egress_origins=origins,
                    egress_rules=rules,
                )
            )
        self.assertEqual(list(origins), process_request["egress_origins"])
        self.assertEqual(list(rules), process_request["egress_rules"])
        self.assertEqual(2, process_request["egress_policy_version"])
        self.assertEqual(
            list(origins),
            json.loads(process_encoded)["egress_origins"],
        )
        self.assertEqual(
            list(rules),
            json.loads(process_encoded)["egress_rules"],
        )

    def test_v3_egress_budget_binding_propagates_to_all_execution_requests(
        self,
    ) -> None:
        rules = ({
            "methods": ["GET", "HEAD"],
            "url_prefix": "https://api.example.test:443/v1/",
        },)
        binding = {
            "budget_scope_sha256": "a" * 64,
            "call_id_sha256": "b" * 64,
        }

        script_request, script_encoded = client.build_skill_script_request(
            skill_root=self.skill,
            workspace=self.workspace,
            entrypoint="scripts/task.sh",
            egress_rules=rules,
            **binding,
        )
        command_request, command_encoded = client.build_declared_command_request(
            skill_root=self.skill,
            workspace=self.workspace,
            executable="python",
            egress_rules=rules,
            **binding,
        )
        scope = client.create_process_owner_scope(
            user_id="egress-user",
            session_id="egress-session",
            root_run_id="egress-run",
        )
        with patch.dict(
            os.environ,
            {"EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN},
        ):
            process_request, process_encoded = (
                client.build_process_lease_open_request(
                    owner_scope=scope,
                    skill_root=self.skill,
                    workspace=self.workspace,
                    entrypoint="scripts/task.sh",
                    egress_rules=rules,
                    **binding,
                )
            )

        for payload, encoded in (
            (script_request, script_encoded),
            (command_request, command_encoded),
            (process_request, process_encoded),
        ):
            with self.subTest(kind=payload["kind"]):
                self.assertEqual(3, payload["egress_policy_version"])
                self.assertEqual(
                    binding["budget_scope_sha256"],
                    payload["budget_scope_sha256"],
                )
                self.assertEqual(
                    binding["call_id_sha256"],
                    payload["call_id_sha256"],
                )
                decoded = json.loads(encoded)
                self.assertEqual(
                    binding["budget_scope_sha256"],
                    decoded["budget_scope_sha256"],
                )

    def test_v3_egress_budget_binding_is_atomic_and_requires_rules(
        self,
    ) -> None:
        rule = [{
            "methods": ["GET"],
            "url_prefix": "https://api.example.test:443/v1/",
        }]
        for kwargs in (
            {
                "egress_rules": rule,
                "budget_scope_sha256": "a" * 64,
            },
            {
                "egress_rules": rule,
                "budget_scope_sha256": "A" * 64,
                "call_id_sha256": "b" * 64,
            },
            {
                "budget_scope_sha256": "a" * 64,
                "call_id_sha256": "b" * 64,
            },
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(client.IsolatedSkillExecutorError):
                    client.build_skill_script_request(
                        skill_root=self.skill,
                        workspace=self.workspace,
                        entrypoint="scripts/task.sh",
                        **kwargs,
                    )

    def test_v3_terminal_audit_is_bound_to_request_authority(self) -> None:
        request, _ = client.build_skill_script_request(
            skill_root=self.skill,
            workspace=self.workspace,
            entrypoint="scripts/task.sh",
            egress_rules=({
                "methods": ["GET", "HEAD"],
                "url_prefix": "https://api.example.test:443/v1/",
            },),
            budget_scope_sha256="a" * 64,
            call_id_sha256="b" * 64,
        )
        authority = {
            "origins": request["egress_origins"],
            "egress_rules": request["egress_rules"],
            "private_origins": request["private_origins"],
        }
        audit = {
            "profile": "bounded_controlled_exchange",
            "version": 1,
            "budget_scope_sha256": "a" * 64,
            "call_id_sha256": "b" * 64,
            "rules_sha256": hashlib.sha256(json.dumps(
                authority,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            "counts": {
                "accepted_connections": 1,
                "client_to_proxy_wire_bytes": 128,
                "proxy_to_client_wire_bytes": 512,
                "budget_rejections": 0,
                "clean_closes": 1,
            },
            "limits": {
                "max_outbound_bytes": 1024,
                "max_requests": 8,
                "max_response_wire_bytes": 4096,
            },
            "exhausted": False,
        }
        audit["receipt_sha256"] = hashlib.sha256(json.dumps(
            audit,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "skill_script_result",
            "request_id": request["request_id"],
            "status": "success",
            "returncode": 0,
            "runtime_profile": client.SESSION_SANDBOX_RUNTIME_PROFILE,
            "network": "disabled",
            "network_policy": {
                "direct": "disabled",
                "egress": "origin_allowlist_proxy",
            },
            "invocation_mode": "cli",
            "artifacts": [],
            "egress_policy_version": 3,
            "egress_audit_receipt": audit,
        }

        self.assertEqual(
            [],
            client.validate_skill_script_response(
                response,
                request_id=request["request_id"],
                request=request,
                invocation_mode="cli",
                expected_egress=True,
            ),
        )
        tampered = dict(response)
        tampered["egress_audit_receipt"] = {
            **audit,
            "call_id_sha256": "c" * 64,
        }
        with self.assertRaises(client.IsolatedSkillExecutorError):
            client.validate_skill_script_response(
                tampered,
                request_id=request["request_id"],
                request=request,
                invocation_mode="cli",
                expected_egress=True,
            )
        missing_execution_evidence = dict(response)
        missing_execution_evidence.pop("returncode")
        missing_execution_evidence.pop("egress_audit_receipt")
        with self.assertRaises(client.IsolatedSkillExecutorError):
            client.validate_skill_script_response(
                missing_execution_evidence,
                request_id=request["request_id"],
                request=request,
                invocation_mode="cli",
                expected_egress=True,
            )
        malformed_returncode = {
            **response,
            "returncode": False,
        }
        with self.assertRaises(client.IsolatedSkillExecutorError):
            client.validate_skill_script_response(
                malformed_returncode,
                request_id=request["request_id"],
                request=request,
                invocation_mode="cli",
                expected_egress=True,
            )

    def test_egress_request_builder_rejects_noncanonical_or_unbounded_values(
        self,
    ) -> None:
        invalid_policies: tuple[object, ...] = (
            ("https://api.example.test",),
            ("https://api.example.test:443/path",),
            ("https://*.example.test:443",),
            (
                "https://api.example.test:443",
                "https://api.example.test:443",
            ),
            ("https://api.example.test:443", 7),
            tuple(
                f"https://host-{index}.example.test:443"
                for index in range(
                    client.MAX_SESSION_SANDBOX_EGRESS_ORIGINS + 1
                )
            ),
        )
        for values in invalid_policies:
            with self.subTest(values=values):
                with self.assertRaises(
                    client.IsolatedSkillExecutorError
                ) as caught:
                    client.build_skill_script_request(
                        skill_root=self.skill,
                        workspace=self.workspace,
                        entrypoint="scripts/task.sh",
                        egress_origins=values,  # type: ignore[arg-type]
                    )
                self.assertEqual(
                    "invalid_egress_policy",
                    caught.exception.code,
                )

    def test_egress_receipts_must_match_the_exact_request_policy(self) -> None:
        request_id = str(uuid.uuid4())
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "skill_script_result",
            "request_id": request_id,
            "status": "success",
            "invocation_mode": "cli",
            "network": "disabled",
            "runtime_profile": "session-sandbox-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "origin_allowlist_proxy",
            },
            "stdout": "",
            "stderr": "",
            "artifacts": [],
        }
        self.assertEqual(
            [],
            client.validate_skill_script_response(
                response,
                request_id=request_id,
                invocation_mode="cli",
                expected_egress=True,
            ),
        )
        response["network_policy"] = {
            "direct": "disabled",
            "egress": "none",
        }
        with self.assertRaises(
            client.IsolatedSkillExecutorError
        ) as caught:
            client.validate_skill_script_response(
                response,
                request_id=request_id,
                invocation_mode="cli",
                expected_egress=True,
            )
        self.assertEqual("invalid_response", caught.exception.code)

        scope = client.create_process_owner_scope(
            user_id="receipt-user",
            session_id="receipt-session",
            root_run_id="receipt-run",
        )
        with patch.dict(
            os.environ,
            {"EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN},
        ):
            process_request, _ = client.build_process_lease_open_request(
                owner_scope=scope,
                skill_root=self.skill,
                workspace=self.workspace,
                entrypoint="scripts/task.sh",
                egress_origins=("https://api.example.test:443",),
                egress_rules=_retrieval_egress_rules(
                    "https://api.example.test:443",
                ),
            )
        process_response = {
            "protocol_version": client.PROCESS_PROTOCOL_VERSION,
            "kind": "process_lease_result",
            "operation": "open",
            "request_id": process_request["request_id"],
            "status": "success",
            "network": "disabled",
            "lease_handle": "pl2_receipt_" + "a" * 32,
            "scope_digest": "b" * 64,
            "skill_sha256": process_request["skill_sha256"],
            "script_sha256": process_request["script_sha256"],
            "state": "open",
            "runtime_profile": "session-sandbox-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "origin_allowlist_proxy",
            },
            "artifacts": [],
        }
        validated, artifacts = client.validate_process_lease_response(
            process_response,
            request=process_request,
        )
        self.assertEqual([], artifacts)
        self.assertEqual("success", validated["status"])

        process_response["network_policy"] = {
            "direct": "disabled",
            "egress": "none",
        }
        with self.assertRaises(
            client.IsolatedSkillExecutorError
        ) as caught:
            client.validate_process_lease_response(
                process_response,
                request=process_request,
            )
        self.assertEqual("invalid_response", caught.exception.code)

    def test_typed_execution_errors_keep_strict_network_attestation(
        self,
    ) -> None:
        request_id = str(uuid.uuid4())
        skill_response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "skill_script_result",
            "request_id": request_id,
            "status": "error",
            "error_code": "worker_busy",
            "error": "The worker is busy.",
            "invocation_mode": "cli",
            "network": "disabled",
            "runtime_profile": client.SESSION_SANDBOX_RUNTIME_PROFILE,
            "network_policy": {
                "direct": "disabled",
                "egress": "origin_allowlist_proxy",
            },
            "artifacts": [],
        }
        self.assertEqual(
            [],
            client.validate_skill_script_response(
                skill_response,
                request_id=request_id,
                invocation_mode="cli",
                expected_egress=True,
            ),
        )
        self.assertEqual("worker_busy", skill_response["error_code"])
        skill_response_without_cause = dict(skill_response)
        skill_response_without_cause.pop("error_code")
        with self.assertRaises(
            client.IsolatedSkillExecutorError
        ) as caught:
            client.validate_skill_script_response(
                skill_response_without_cause,
                request_id=request_id,
                invocation_mode="cli",
                expected_egress=True,
            )
        self.assertEqual("invalid_response", caught.exception.code)

        command_request, _ = client.build_declared_command_request(
            skill_root=self.skill,
            workspace=self.workspace,
            executable="python",
            argv=["-V"],
            egress_origins=("https://api.example.test:443",),
            egress_rules=_retrieval_egress_rules(
                "https://api.example.test:443",
            ),
        )
        command_digest = hashlib.sha256(json.dumps(
            [
                command_request["executable"],
                *command_request["argv"],
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        command_response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "declared_command_result",
            "request_id": command_request["request_id"],
            "status": "error",
            "error_code": "command_spawn_failed",
            "error": "The command could not start.",
            "executable": command_request["executable"],
            "cwd": command_request["cwd"],
            "command_sha256": command_digest,
            "shell": False,
            "network": "disabled",
            "runtime_profile": client.SESSION_SANDBOX_RUNTIME_PROFILE,
            "network_policy": {
                "direct": "disabled",
                "egress": "origin_allowlist_proxy",
            },
            "artifacts": [],
        }
        self.assertEqual(
            [],
            client.validate_declared_command_response(
                command_response,
                request=command_request,
            ),
        )
        self.assertEqual(
            "command_spawn_failed",
            command_response["error_code"],
        )

        command_response["network_policy"] = {
            "direct": "disabled",
            "egress": "none",
        }
        with self.assertRaises(
            client.IsolatedSkillExecutorError
        ) as caught:
            client.validate_declared_command_response(
                command_response,
                request=command_request,
            )
        self.assertEqual("invalid_response", caught.exception.code)

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

    def test_persistent_v3_bound_error_without_audit_stays_typed_and_live(
        self,
    ) -> None:
        scope = client.create_process_owner_scope(
            user_id="bound-error-user",
            session_id="bound-error-session",
            root_run_id="bound-error-run",
        )

        class RecordingReservation:
            def __init__(self) -> None:
                self.terminal = False
                self.quarantine_reasons: list[str] = []

            async def quarantine(self, reason: str) -> None:
                self.quarantine_reasons.append(reason)
                self.terminal = True

            async def release(self) -> None:
                self.terminal = True

        reservation = RecordingReservation()
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
            _egress_policy_version=3,
            _budget_scope_sha256="b" * 64,
            _call_id_sha256="c" * 64,
            _egress_authority_sha256="d" * 64,
            _slot_reservation=reservation,
        )

        async def exchange(
            payload: dict[str, object],
            _encoded: bytes,
            **_kwargs: object,
        ):
            response = {
                "protocol_version": client.PROCESS_PROTOCOL_VERSION,
                "kind": "process_lease_result",
                "operation": payload["operation"],
                "request_id": payload["request_id"],
                "status": "error",
                "error_code": "process_already_started",
                "error": "The exact lease entrypoint can only be started once.",
                "network": "disabled",
                "lease_handle": lease.handle,
                "scope_digest": "e" * 64,
                "skill_sha256": lease.skill_sha256,
                "script_sha256": lease.script_sha256,
                "state": "running",
                "artifacts": [],
                "runtime_profile": client.SESSION_SANDBOX_RUNTIME_PROFILE,
                "network_policy": {
                    "direct": "disabled",
                    "egress": "origin_allowlist_proxy",
                },
                "egress_policy_version": 3,
            }
            return client.validate_process_lease_response(
                response,
                request=payload,
            )

        with patch.dict(
            os.environ,
            {"EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN},
        ), patch.object(
            client,
            "_exchange_process_request",
            side_effect=exchange,
        ):
            with self.assertRaises(
                client.IsolatedSkillExecutorError
            ) as caught:
                asyncio.run(
                    client.start_isolated_process_lease(lease)
                )

        self.assertEqual(
            "process_already_started",
            caught.exception.code,
        )
        self.assertFalse(caught.exception.dispatch_unknown)
        self.assertIsNone(caught.exception.terminal_lease_state)
        self.assertFalse(lease.closed)
        self.assertIs(lease._slot_reservation, reservation)
        self.assertFalse(reservation.terminal)
        self.assertEqual([], reservation.quarantine_reasons)

    def test_mismatched_pending_ack_quarantines_without_clearing_transaction(
        self,
    ) -> None:
        scope = client.create_process_owner_scope(
            user_id="ack-mismatch-user",
            session_id="ack-mismatch-session",
            root_run_id="ack-mismatch-run",
        )

        class RecordingReservation:
            def __init__(self) -> None:
                self.terminal = False
                self.quarantine_reasons: list[str] = []

            async def quarantine(self, reason: str) -> None:
                self.quarantine_reasons.append(reason)
                self.terminal = True

            async def release(self) -> None:
                self.terminal = True

        reservation = RecordingReservation()
        prepare_op_id = str(uuid.uuid4())
        ack_op_id = str(uuid.uuid4())
        prepared = {
            "sync_token": "q" * 43,
            "sync_pending": True,
            "state": "running",
        }
        lease = client.IsolatedProcessLease(
            handle="pl2_" + "1" * 32 + "_" + "2" * 32,
            skill_sha256="3" * 64,
            script_sha256="4" * 64,
            entrypoint="scripts/task.sh",
            invocation_mode="cli",
            class_name=None,
            factory_name=None,
            _owner_scope=scope,
            _workspace=self.workspace,
            _socket_path="/unused",
            _baseline={},
            _egress_policy_version=3,
            _budget_scope_sha256="5" * 64,
            _call_id_sha256="6" * 64,
            _egress_authority_sha256="7" * 64,
            _slot_reservation=reservation,
            _pending_sync_operation="sync",
            _pending_sync_prepare_op_id=prepare_op_id,
            _pending_sync_response=dict(prepared),
            _pending_sync_artifacts=[],
            _pending_sync_ack_op_id=ack_op_id,
            _pending_sync_applied=[],
            _pending_sync_apply_artifacts=True,
        )

        async def exchange(
            payload: dict[str, object],
            _encoded: bytes,
            **_kwargs: object,
        ):
            response = {
                "protocol_version": client.PROCESS_PROTOCOL_VERSION,
                "kind": "process_lease_result",
                "operation": "ack",
                "request_id": payload["request_id"],
                "status": "success",
                "network": "disabled",
                "lease_handle": lease.handle,
                "scope_digest": "8" * 64,
                "skill_sha256": lease.skill_sha256,
                "script_sha256": lease.script_sha256,
                "state": "closed",
                "runtime_profile": client.SESSION_SANDBOX_RUNTIME_PROFILE,
                "network_policy": {
                    "direct": "disabled",
                    "egress": "origin_allowlist_proxy",
                },
                "egress_policy_version": 3,
                "sync_acknowledged": True,
                "acknowledged_operation": "close",
            }
            return client.validate_process_lease_response(
                response,
                request=payload,
            )

        with patch.dict(
            os.environ,
            {"EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN},
        ), patch.object(
            client,
            "_exchange_process_request",
            side_effect=exchange,
        ):
            with self.assertRaises(
                client.IsolatedSkillExecutorError
            ) as caught:
                asyncio.run(
                    client._finish_pending_process_sync(lease)
                )

        self.assertEqual("invalid_response", caught.exception.code)
        self.assertEqual(
            "quarantined",
            caught.exception.terminal_lease_state,
        )
        self.assertTrue(lease.closed)
        self.assertTrue(reservation.terminal)
        self.assertEqual(
            ["invalid_egress_audit_response"],
            reservation.quarantine_reasons,
        )
        self.assertIsNone(lease._slot_reservation)
        self.assertEqual("sync", lease._pending_sync_operation)
        self.assertEqual(
            prepare_op_id,
            lease._pending_sync_prepare_op_id,
        )
        self.assertEqual(prepared, lease._pending_sync_response)
        self.assertEqual([], lease._pending_sync_artifacts)
        self.assertEqual(ack_op_id, lease._pending_sync_ack_op_id)
        self.assertEqual([], lease._pending_sync_applied)
        self.assertIs(lease._pending_sync_apply_artifacts, True)

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

    def test_cancelled_slot_release_keeps_replayable_close_result(self) -> None:
        scope = client.create_process_owner_scope(
            user_id="close-cancel-user",
            session_id="close-cancel-session",
            root_run_id="close-cancel-run",
        )

        class CancelledTerminalReservation:
            def __init__(self) -> None:
                self.terminal = False
                self.release_calls = 0

            async def release(self) -> None:
                self.release_calls += 1
                self.terminal = True
                raise asyncio.CancelledError

        reservation = CancelledTerminalReservation()
        lease = client.IsolatedProcessLease(
            handle="pl2_" + "1" * 32 + "_" + "2" * 32,
            skill_sha256="3" * 64,
            script_sha256="4" * 64,
            entrypoint="scripts/task.sh",
            invocation_mode="cli",
            class_name=None,
            factory_name=None,
            _owner_scope=scope,
            _workspace=self.workspace,
            _socket_path="/unused",
            _baseline={},
            _slot_reservation=reservation,
        )
        prepared = {
            "sync_token": "v" * 43,
            "sync_pending": True,
            "state": "closing",
        }
        acknowledged = {
            "state": "closed",
            "acknowledged_operation": "close",
            "sync_acknowledged": True,
        }
        execute = AsyncMock(side_effect=[
            (prepared, []),
            (acknowledged, []),
        ])

        with patch.object(client, "_execute_process_operation", execute):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(client.close_isolated_process_lease(lease))
            replayed = asyncio.run(
                client.close_isolated_process_lease(lease)
            )

        self.assertTrue(lease.closed)
        self.assertIsNone(lease._slot_reservation)
        self.assertEqual("closed", replayed["state"])
        self.assertTrue(replayed["sync_acknowledged"])
        self.assertEqual(1, reservation.release_calls)
        self.assertEqual(2, execute.await_count)

    def test_process_exchange_marks_post_write_cancellation_unknown(self) -> None:
        class BlockingReader:
            def __init__(self) -> None:
                self.waiting = asyncio.Event()
                self.release = asyncio.Event()

            async def readline(self) -> bytes:
                self.waiting.set()
                await self.release.wait()
                return b""

        class Writer:
            def __init__(self) -> None:
                self.written = False
                self.closed = False

            def write(self, _encoded: bytes) -> None:
                self.written = True

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        async def scenario() -> BaseException:
            reader = BlockingReader()
            writer = Writer()
            with patch.object(
                client.asyncio,
                "open_unix_connection",
                new=AsyncMock(return_value=(reader, writer)),
            ):
                exchange = asyncio.create_task(
                    client._exchange_process_request(
                        {"operation": "write"},
                        b"{}\n",
                        socket_path="/fixture/process.sock",
                        timeout=30,
                    )
                )
                await asyncio.wait_for(reader.waiting.wait(), timeout=0.2)
                exchange.cancel()
                try:
                    await exchange
                except asyncio.CancelledError as exc:
                    self.assertTrue(writer.written)
                    self.assertTrue(writer.closed)
                    return exc
            self.fail("post-write process exchange cancellation was swallowed")

        cancellation = asyncio.run(scenario())
        self.assertIsInstance(cancellation, asyncio.CancelledError)
        self.assertTrue(getattr(cancellation, "dispatch_unknown", False))

    def test_pool_reservation_mismatch_stays_typed_and_retained(
        self,
    ) -> None:
        scope = client.create_process_owner_scope(
            user_id="mismatch-user",
            session_id="mismatch-session",
            root_run_id="mismatch-run",
        )

        class MismatchedReservation:
            terminal = False

            async def release(self) -> None:
                raise ExecutorSlotPoolError(
                    "executor_pool_reservation_mismatch",
                    "fixture authoritative reservation mismatch",
                )

        reservation = MismatchedReservation()
        lease = client.IsolatedProcessLease(
            handle="pl2_" + "5" * 32 + "_" + "6" * 32,
            skill_sha256="7" * 64,
            script_sha256="8" * 64,
            entrypoint="scripts/task.sh",
            invocation_mode="cli",
            class_name=None,
            factory_name=None,
            _owner_scope=scope,
            _workspace=self.workspace,
            _socket_path="/unused",
            _baseline={},
            _slot_reservation=reservation,
        )
        terminal_error = client.IsolatedSkillExecutorError(
            "lease_not_found",
            "fixture server-proven terminal lease",
            terminal_lease_state="closed",
        )

        with self.assertRaises(
            client.IsolatedSkillExecutorError
        ) as caught:
            asyncio.run(client.finalize_terminal_process_lease_error(
                lease,
                terminal_error,
            ))

        self.assertEqual(
            "executor_pool_reservation_mismatch",
            caught.exception.code,
        )
        self.assertFalse(lease.closed)
        self.assertIs(lease._slot_reservation, reservation)
        self.assertFalse(reservation.terminal)

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
        self.assertEqual([], request["egress_origins"])
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
            "runtime_profile": "session-sandbox-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "none",
            },
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

    def test_declared_command_egress_request_and_receipt_are_exact(self) -> None:
        origins = (
            "https://api.example.test:443",
            "http://catalog.example.test:80",
        )
        request, encoded = client.build_declared_command_request(
            skill_root=self.skill,
            workspace=self.workspace,
            executable="python",
            egress_origins=origins,
            egress_rules=_retrieval_egress_rules(*origins),
        )
        self.assertEqual(list(origins), request["egress_origins"])
        self.assertEqual(
            list(_retrieval_egress_rules(*origins)),
            request["egress_rules"],
        )
        self.assertEqual(
            list(origins),
            json.loads(encoded)["egress_origins"],
        )
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "declared_command_result",
            "request_id": request["request_id"],
            "status": "success",
            "executable": "python",
            "cwd": "workspace",
            "network": "disabled",
            "runtime_profile": "session-sandbox-v1",
            "network_policy": {
                "direct": "disabled",
                "egress": "origin_allowlist_proxy",
            },
            "shell": False,
            "command_sha256": hashlib.sha256(
                json.dumps(
                    ["python"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "stdout": "",
            "stderr": "",
            "artifacts": [],
        }
        self.assertEqual(
            [],
            client.validate_declared_command_response(
                response,
                request=request,
            ),
        )

        response["network_policy"] = {
            "direct": "disabled",
            "egress": "none",
        }
        with self.assertRaises(
            client.IsolatedSkillExecutorError
        ) as caught:
            client.validate_declared_command_response(
                response,
                request=request,
            )
        self.assertEqual("invalid_response", caught.exception.code)

        with self.assertRaises(
            client.IsolatedSkillExecutorError
        ) as caught:
            client.build_declared_command_request(
                skill_root=self.skill,
                workspace=self.workspace,
                executable="python",
                egress_origins=("https://*.example.test:443",),
            )
        self.assertEqual("invalid_egress_policy", caught.exception.code)

    def test_declared_command_v3_success_cannot_omit_execution_evidence(self) -> None:
        request, _ = client.build_declared_command_request(
            skill_root=self.skill,
            workspace=self.workspace,
            executable="python",
            egress_rules=_retrieval_egress_rules(
                "https://api.example.test:443",
            ),
            budget_scope_sha256="a" * 64,
            call_id_sha256="b" * 64,
        )
        response = {
            "protocol_version": client.PROTOCOL_VERSION,
            "kind": "declared_command_result",
            "request_id": request["request_id"],
            "status": "success",
            "executable": request["executable"],
            "cwd": request["cwd"],
            "network": "disabled",
            "runtime_profile": client.SESSION_SANDBOX_RUNTIME_PROFILE,
            "network_policy": {
                "direct": "disabled",
                "egress": "origin_allowlist_proxy",
            },
            "shell": False,
            "command_sha256": hashlib.sha256(json.dumps(
                [request["executable"], *request["argv"]],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            "stdout": "",
            "stderr": "",
            "artifacts": [],
            "egress_policy_version": 3,
        }
        with self.assertRaises(
            client.IsolatedSkillExecutorError
        ) as caught:
            client.validate_declared_command_response(
                response,
                request=request,
            )
        self.assertEqual("invalid_response", caught.exception.code)

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
        self.assertNotIn("egress_origins", payload)
        self.assertEqual(["input.bin"], [item["path"] for item in payload["workspace_files"]])
        self.assertEqual(
            {"SKILL.md", "assets/binary.bin", "scripts/task.sh"},
            {item["path"] for item in payload["skill_files"]},
        )
        self.assertEqual([], payload["result_files"])

    def test_session_code_request_snapshots_only_selected_persisted_results(self) -> None:
        results = self.root / "results"
        results.mkdir()
        (results / "selected.txt").write_bytes(b"selected")
        (results / "unselected.txt").write_bytes(b"unselected")

        payload, _ = client.build_session_code_request(
            workspace=self.workspace,
            results_root=results,
            result_paths=["selected.txt"],
            code="print(open('../results/selected.txt').read())",
        )

        self.assertEqual(
            ["selected.txt"],
            [item["path"] for item in payload["result_files"]],
        )
        self.assertEqual(
            b"selected",
            base64.b64decode(payload["result_files"][0]["content_b64"]),
        )

    def test_session_code_result_snapshot_rejects_traversal_and_symlinks(self) -> None:
        results = self.root / "results"
        results.mkdir()
        (results / "target.txt").write_text("target", encoding="utf-8")
        (results / "link.txt").symlink_to(results / "target.txt")

        for selected in (["../target.txt"], ["link.txt"]):
            with self.subTest(selected=selected):
                with self.assertRaises(client.IsolatedSkillExecutorError) as caught:
                    client.build_session_code_request(
                        workspace=self.workspace,
                        results_root=results,
                        result_paths=selected,
                        code="print('never dispatched')",
                    )
                self.assertIn(caught.exception.code, {"invalid_path", "unsafe_snapshot_file"})

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
