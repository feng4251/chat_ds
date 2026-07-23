from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import sys
import time
import unittest
import uuid
from unittest.mock import patch

from executor import server


V2_AUTH_TOKEN = "test-only-v2-auth-token-" + "x" * 32


def _file(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _skill_digest(files: list[dict[str, object]]) -> str:
    manifest = [
        {
            "path": item["path"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in sorted(files, key=lambda item: str(item["path"]))
    ]
    return hashlib.sha256(json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _owner_scope(
    *,
    user_id: str = "user-1",
    session_id: str = "session-1",
    root_run_id: str = "run-1",
    authority_token: str = "a" * 43,
) -> dict[str, str]:
    return {
        "user_id": user_id,
        "session_id": session_id,
        "root_run_id": root_run_id,
        "authority_token": authority_token,
    }


def _sign_v2(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    result.pop("auth_hmac", None)
    canonical = json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    result["auth_hmac"] = hmac.new(
        V2_AUTH_TOKEN.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return result


def _process_open_request(
    script: bytes,
    *,
    entrypoint: str = "scripts/task.py",
    owner_scope: dict[str, str] | None = None,
    invocation: dict[str, object] | None = None,
    idle_ttl_seconds: int = 30,
) -> dict[str, object]:
    skill_files = [
        _file("SKILL.md", b"---\nname: process-fixture\n---\n"),
        _file(entrypoint, script),
    ]
    return _sign_v2({
        "protocol_version": server.PROCESS_PROTOCOL_VERSION,
        "kind": "process_lease",
        "operation": "open",
        "request_id": str(uuid.uuid4()),
        "op_id": str(uuid.uuid4()),
        "owner_scope": owner_scope or _owner_scope(),
        "entrypoint": entrypoint,
        "argv": [],
        "invocation": invocation or {"mode": "cli"},
        "cwd": "workspace",
        "idle_ttl_seconds": idle_ttl_seconds,
        "max_runtime_seconds": 60,
        "skill_sha256": _skill_digest(skill_files),
        "script_sha256": hashlib.sha256(script).hexdigest(),
        "skill_files": skill_files,
        "workspace_files": [_file("input.txt", b"initial")],
    })


def _process_operation(
    opened: dict[str, object],
    operation: str,
    *,
    owner_scope: dict[str, str] | None = None,
    op_id: str | None = None,
    **extra: object,
) -> dict[str, object]:
    return _sign_v2({
        "protocol_version": server.PROCESS_PROTOCOL_VERSION,
        "kind": "process_lease",
        "operation": operation,
        "request_id": str(uuid.uuid4()),
        "op_id": op_id or str(uuid.uuid4()),
        "owner_scope": owner_scope or _owner_scope(),
        "lease_handle": opened["lease_handle"],
        "skill_sha256": opened["skill_sha256"],
        "script_sha256": opened["script_sha256"],
        **extra,
    })


def _ack_process_batch(
    opened: dict[str, object],
    prepared: dict[str, object],
    *,
    op_id: str | None = None,
) -> dict[str, object]:
    return server._run(_process_operation(
        opened,
        "ack",
        op_id=op_id,
        sync_token=prepared["sync_token"],
    ))


def _request(script: bytes, *, entrypoint: str = "scripts/task.py") -> dict[str, object]:
    return {
        "protocol_version": server.PROTOCOL_VERSION,
        "kind": "skill_script",
        "request_id": str(uuid.uuid4()),
        "entrypoint": entrypoint,
        "argv": ["literal $(touch never)"],
        "timeout": 5,
        "cwd": "workspace",
        "skill_files": [
            _file("SKILL.md", b"---\nname: portable-fixture\n---\n"),
            _file(entrypoint, script),
            _file("assets/payload.bin", b"\x00\xffbinary"),
        ],
        "workspace_files": [
            _file("input.txt", b"before"),
            _file("unchanged.txt", b"same"),
        ],
    }


def _command_request(*, executable: str = "python") -> dict[str, object]:
    code = (
        "import os,sys; from pathlib import Path; "
        "Path(os.environ['CHATDS_WORKSPACE']).joinpath('command.txt').write_text(sys.argv[1]); "
        "print(sys.argv[1])"
    )
    return {
        "protocol_version": server.PROTOCOL_VERSION,
        "kind": "declared_command",
        "request_id": str(uuid.uuid4()),
        "executable": executable,
        "argv": ["-c", code, "literal; $(touch never) | ignored"],
        "timeout": 5,
        "cwd": "workspace",
        "skill_files": [_file("SKILL.md", b"---\nname: command-fixture\n---\n")],
        "workspace_files": [],
    }


class SkillScriptServerTests(unittest.TestCase):
    def test_immutable_snapshot_executes_only_supported_script_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as text:
            temp_root = Path(text) / "lease"
            temp_root.mkdir()
            skill_root = temp_root / "skill"
            server._materialize_snapshot(
                skill_root,
                {
                    "SKILL.md": b"---\nname: mode-fixture\n---\n",
                    "scripts/helper.sh": b"#!/bin/bash\nexit 0\n",
                    "scripts/helper.PY": b"#!/usr/bin/python3\n",
                    "assets/input.json": b"{}\n",
                },
                immutable=True,
            )
            self.assertEqual(
                0o555,
                (skill_root / "scripts/helper.sh").stat().st_mode & 0o777,
            )
            self.assertEqual(
                0o555,
                (skill_root / "scripts/helper.PY").stat().st_mode & 0o777,
            )
            self.assertEqual(
                0o444,
                (skill_root / "SKILL.md").stat().st_mode & 0o777,
            )
            self.assertEqual(
                0o444,
                (skill_root / "assets/input.json").stat().st_mode & 0o777,
            )

            with patch.dict(
                os.environ,
                {
                    "EXECUTOR_WORKER_UID": "65529",
                    "EXECUTOR_WORKER_GID": "65529",
                },
            ), patch.object(server.os, "chown"):
                server._prepare_worker_tree(
                    temp_root,
                    immutable_roots=(skill_root,),
                )
            self.assertEqual(
                0o550,
                (skill_root / "scripts/helper.sh").stat().st_mode & 0o777,
            )
            self.assertEqual(
                0o550,
                (skill_root / "scripts/helper.PY").stat().st_mode & 0o777,
            )
            self.assertEqual(
                0o440,
                (skill_root / "SKILL.md").stat().st_mode & 0o777,
            )
            self.assertEqual(
                0o440,
                (skill_root / "assets/input.json").stat().st_mode & 0o777,
            )

    def test_declared_command_uses_literal_argv_without_shell_and_receipts_artifact(self) -> None:
        request = _command_request()
        with patch.object(server.shutil, "which", return_value=sys.executable):
            response = server._run_declared_command(request)

        self.assertEqual("success", response["status"])
        self.assertIs(response["shell"], False)
        self.assertEqual("disabled", response["network"])
        self.assertEqual("literal; $(touch never) | ignored\n", response["stdout"])
        expected_receipt = hashlib.sha256(json.dumps(
            [request["executable"], *request["argv"]],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.assertEqual(expected_receipt, response["command_sha256"])
        artifact = response["artifacts"][0]
        self.assertEqual("command.txt", artifact["path"])
        self.assertEqual(
            b"literal; $(touch never) | ignored",
            base64.b64decode(artifact["content_b64"]),
        )

    def test_declared_command_rejects_paths_and_reports_missing_runtime_command(self) -> None:
        path_request = _command_request(executable="/bin/sh")
        path_response = server._run_declared_command(path_request)
        self.assertEqual("invalid_executable", path_response["error_code"])

        request = _command_request(executable="missing-command")
        with patch.object(server.shutil, "which", return_value=None):
            response = server._run_declared_command(request)
        self.assertEqual("command_unavailable", response["error_code"])
        self.assertEqual([], response["artifacts"])

    def test_runtime_capabilities_use_exact_sidecar_packages_and_sanitized_environment(self) -> None:
        request_id = str(uuid.uuid4())
        with patch.object(
            server.shutil,
            "which",
            side_effect=lambda name, **_: "/usr/local/bin/python" if name == "python" else None,
        ):
            response = server._run_runtime_capabilities({
                "protocol_version": server.PROTOCOL_VERSION,
                "kind": "runtime_capabilities",
                "request_id": request_id,
                "requirements": [
                    "packaging>=20",
                    "chatds-definitely-absent-package>=1",
                ],
                "commands": ["python", "chatds-definitely-absent-command"],
                "environment_variables": [
                    "CHATDS_WORKSPACE",
                    "SKILL_DIR",
                    "INTERNAL_API_TOKEN",
                ],
                "platform_groups": [["linux"], ["windows"]],
            })

        self.assertEqual("runtime_capabilities_result", response["kind"])
        self.assertEqual(request_id, response["request_id"])
        self.assertFalse(response["valid"])
        packages = {item["requirement"]: item for item in response["requirements"]}
        self.assertTrue(packages["packaging>=20"]["satisfied"])
        self.assertEqual(
            "missing",
            packages["chatds-definitely-absent-package>=1"]["status"],
        )
        commands = {item["name"]: item["available"] for item in response["commands"]}
        self.assertTrue(commands["python"])
        self.assertFalse(commands["chatds-definitely-absent-command"])
        environment = {
            item["name"]: item["available"]
            for item in response["environment_variables"]
        }
        self.assertTrue(environment["CHATDS_WORKSPACE"])
        self.assertTrue(environment["SKILL_DIR"])
        self.assertFalse(environment["INTERNAL_API_TOKEN"])
        self.assertTrue(all(
            set(item) == {"name", "available"}
            for item in response["environment_variables"]
        ))
        self.assertTrue(response["platform_groups"][0]["satisfied"])
        self.assertFalse(response["platform_groups"][1]["satisfied"])
        self.assertEqual("disabled", response["runtime_identity"]["dependency_install"])

    def test_runtime_capability_request_rejects_direct_references_without_installing(self) -> None:
        response = server._run_runtime_capabilities({
            "protocol_version": server.PROTOCOL_VERSION,
            "kind": "runtime_capabilities",
            "request_id": str(uuid.uuid4()),
            "requirements": ["demo @ https://example.invalid/demo.whl"],
            "commands": [],
            "environment_variables": [],
            "platform_groups": [],
        })

        self.assertEqual("success", response["status"])
        self.assertFalse(response["valid"])
        self.assertEqual(
            "unsupported_direct_reference",
            response["requirements"][0]["status"],
        )

    def test_python_snapshot_executes_and_returns_only_changed_regular_files(self) -> None:
        script = b"""
import os
from pathlib import Path
import sys

workspace = Path(os.environ["CHATDS_WORKSPACE"])
skill = Path(os.environ["CHATDS_SKILL_DIR"])
payload = (skill / "assets" / "payload.bin").read_bytes()
(workspace / "input.txt").write_bytes(b"after")
(Path(os.environ["CHATDS_OUTPUT_DIR"]) / "result.bin").write_bytes(payload)
print(sys.argv[1])
"""
        response = server._run_skill_script(_request(script))

        self.assertEqual("success", response["status"])
        self.assertEqual("python", response["interpreter"])
        self.assertEqual("disabled", response["network"])
        self.assertEqual("literal $(touch never)\n", response["stdout"])
        artifacts = {item["path"]: item for item in response["artifacts"]}
        self.assertEqual({"input.txt", "output_result/result.bin"}, set(artifacts))
        self.assertEqual("modified", artifacts["input.txt"]["change"])
        binary = artifacts["output_result/result.bin"]
        self.assertEqual(b"\x00\xffbinary", base64.b64decode(binary["content_b64"]))
        self.assertEqual(hashlib.sha256(b"\x00\xffbinary").hexdigest(), binary["sha256"])

    def test_python_cli_imports_only_helpers_from_the_exact_skill_snapshot(self) -> None:
        request = _request(
            b"from sibling import SIBLING\n"
            b"from helpers.value import PACKAGE\n"
            b"print(f'{SIBLING}:{PACKAGE}')\n"
        )
        request["skill_files"].extend([
            _file("scripts/sibling.py", b"SIBLING = 'sibling'\n"),
            _file("helpers/__init__.py", b""),
            _file("helpers/value.py", b"PACKAGE = 'package'\n"),
        ])

        response = server._run_skill_script(request)

        self.assertEqual("success", response["status"])
        self.assertEqual("python", response["interpreter"])
        self.assertEqual("sibling:package", response["stdout"].strip())
        self.assertEqual("disabled", response["network"])

    def test_python_cli_isolated_path_excludes_workspace_and_launcher_locations(self) -> None:
        request = _request(
            b"import json, os, sys\n"
            b"from pathlib import Path\n"
            b"workspace = Path(os.environ['CHATDS_WORKSPACE']).resolve()\n"
            b"skill = Path(os.environ['CHATDS_SKILL_DIR']).resolve()\n"
            b"paths = [Path(item).resolve() for item in sys.path if item]\n"
            b"print(json.dumps({\n"
            b"  'workspace_visible': workspace in paths,\n"
            b"  'skill_parent_visible': skill.parent in paths,\n"
            b"  'skill_visible': skill in paths,\n"
            b"  'script_visible': Path(__file__).resolve().parent in paths,\n"
            b"}))\n"
        )
        request["workspace_files"].append(
            _file("workspace_only.py", b"VALUE = 'must-not-import'\n")
        )

        response = server._run_skill_script(request)

        self.assertEqual("success", response["status"])
        paths = json.loads(response["stdout"])
        self.assertFalse(paths["workspace_visible"])
        self.assertFalse(paths["skill_parent_visible"])
        self.assertTrue(paths["skill_visible"])
        self.assertTrue(paths["script_visible"])

        blocked = _request(b"import workspace_only\n")
        blocked["workspace_files"].append(
            _file("workspace_only.py", b"VALUE = 'must-not-import'\n")
        )
        blocked_response = server._run_skill_script(blocked)
        self.assertEqual("error", blocked_response["status"])
        self.assertEqual("script_exit_nonzero", blocked_response["error_code"])
        self.assertIn("ModuleNotFoundError", blocked_response["stderr"])

    def test_commonjs_skill_entrypoint_uses_fixed_node_interpreter(self) -> None:
        request = _request(
            b'"use strict";\nconsole.log(process.argv[2]);\n',
            entrypoint="scripts/task.cjs",
        )

        # The production executor runs as its own low-process-count UID.  The
        # checkout test runner shares a busy developer UID, so applying the
        # production RLIMIT_NPROC there can prevent Node from creating even
        # its fixed worker threads before the fixture starts.
        with patch.object(
            server,
            "_resource_limited_command",
            side_effect=lambda command, **_: command,
        ):
            response = server._run_skill_script(request)

        self.assertEqual("success", response["status"])
        self.assertEqual("node", response["interpreter"])
        self.assertEqual("literal $(touch never)", response["stdout"].strip())
        self.assertEqual("disabled", response["network"])

    def test_links_and_special_output_fail_closed_without_artifacts(self) -> None:
        scripts = (
            b"""
import os
from pathlib import Path
workspace = Path(os.environ["CHATDS_WORKSPACE"])
target = workspace / "ordinary.txt"
target.write_text("data", encoding="utf-8")
os.symlink(target, workspace / "linked.txt")
""",
            b"""
import os
from pathlib import Path
workspace = Path(os.environ["CHATDS_WORKSPACE"])
target = workspace / "ordinary.txt"
target.write_text("data", encoding="utf-8")
os.link(target, workspace / "hardlinked.txt")
""",
            b"""
import os
from pathlib import Path
os.mkfifo(Path(os.environ["CHATDS_WORKSPACE"]) / "named-pipe")
""",
        )
        for script in scripts:
            with self.subTest(script=script.splitlines()[-2]):
                response = server._run_skill_script(_request(script))
                self.assertEqual("error", response["status"])
                self.assertEqual("unsafe_workspace_output", response["error_code"])
                self.assertEqual([], response["artifacts"])

    def test_traversal_and_snapshot_hash_mismatch_are_rejected_before_execution(self) -> None:
        traversal = _request(b"raise SystemExit('must not run')\n")
        traversal["entrypoint"] = "../task.py"
        response = server._run_skill_script(traversal)
        self.assertEqual("invalid_path", response["error_code"])

        mismatch = _request(b"raise SystemExit('must not run')\n")
        mismatch["skill_files"][0]["sha256"] = "0" * 64
        response = server._run_skill_script(mismatch)
        self.assertEqual("snapshot_integrity_error", response["error_code"])

    def test_stdout_is_continuously_drained_but_bounded(self) -> None:
        script = b"import sys\nsys.stdout.write('x' * 10000)\n"
        with patch.object(server, "MAX_STDOUT_BYTES", 64):
            response = server._run_skill_script(_request(script))

        self.assertEqual("success", response["status"])
        self.assertEqual(64, len(response["stdout"].encode("utf-8")))
        self.assertTrue(response["stdout_truncated"])

    def test_existing_top_level_function_mode_remains_compatible(self) -> None:
        request = _request(
            b"def search(query, limit=2):\n"
            b"    return [query] * limit\n"
        )
        request["argv"] = []
        request["invocation"] = {
            "mode": "function",
            "name": "search",
            "args": ["target"],
            "kwargs": {"limit": 3},
        }

        response = server._run_skill_script(request)

        self.assertEqual("success", response["status"])
        self.assertEqual("function", response["invocation_mode"])
        self.assertEqual("search", response["function_name"])
        self.assertEqual(["target", "target", "target"], response["result"])

    def test_public_instance_method_runs_with_bounded_constructor_and_identity_receipt(self) -> None:
        script = b"""
from dataclasses import dataclass

@dataclass
class QueryClient:
    prefix: str
    use_cache: bool = False

    async def search(self, query, limit=2):
        print(f"search:{query}")
        return {
            "query": self.prefix + query,
            "limit": limit,
            "use_cache": self.use_cache,
        }
"""
        request = _request(script)
        request["argv"] = []
        request["invocation"] = {
            "mode": "instance_method",
            "class_name": "QueryClient",
            "method_name": "search",
            "constructor_args": ["prefix:"],
            "constructor_kwargs": {"use_cache": False},
            "method_args": ["target"],
            "method_kwargs": {"limit": 3},
        }

        response = server._run_skill_script(request)

        self.assertEqual("success", response["status"])
        self.assertEqual("instance_method", response["invocation_mode"])
        self.assertEqual("QueryClient", response["class_name"])
        self.assertEqual("search", response["method_name"])
        self.assertEqual("search:target", response["stdout"].strip())
        self.assertEqual({
            "query": "prefix:target",
            "limit": 3,
            "use_cache": False,
        }, response["result"])
        self.assertEqual([], response["artifacts"])

    def test_instance_method_has_ast_and_runtime_identity_gates(self) -> None:
        rejected_sources = {
            "imported class": (
                b"from collections import Counter\n",
                "Counter",
                "most_common",
            ),
            "static method": (
                b"class Query:\n"
                b"    @staticmethod\n"
                b"    def search(value):\n"
                b"        return value\n",
                "Query",
                "search",
            ),
            "decorated method": (
                b"def passthrough(fn):\n"
                b"    return fn\n"
                b"class Query:\n"
                b"    @passthrough\n"
                b"    def search(self, value):\n"
                b"        return value\n",
                "Query",
                "search",
            ),
        }
        for label, (script, class_name, method_name) in rejected_sources.items():
            with self.subTest(label=label):
                request = _request(script)
                request["argv"] = []
                request["invocation"] = {
                    "mode": "instance_method",
                    "class_name": class_name,
                    "method_name": method_name,
                    "constructor_args": [],
                    "constructor_kwargs": {},
                    "method_args": [],
                    "method_kwargs": {},
                }
                response = server._run_skill_script(request)
                self.assertEqual("error", response["status"])
                self.assertEqual("invalid_function_call", response["error_code"])

        runtime_rebound = _request(
            b"class Query:\n"
            b"    def search(self, value):\n"
            b"        return value\n"
            b"Query = dict\n"
        )
        runtime_rebound["argv"] = []
        runtime_rebound["invocation"] = {
            "mode": "instance_method",
            "class_name": "Query",
            "method_name": "search",
            "constructor_args": [],
            "constructor_kwargs": {},
            "method_args": ["must-not-run"],
            "method_kwargs": {},
        }
        response = server._run_skill_script(runtime_rebound)
        self.assertEqual("error", response["status"])
        self.assertEqual("instance_method_exception", response["error_code"])
        self.assertEqual("Query", response["class_name"])
        self.assertEqual("search", response["method_name"])
        self.assertIn("Imported or replaced classes", response["error"])

    def test_instance_method_protocol_rejects_cli_mixing_and_shared_bound_overflow(self) -> None:
        script = (
            b"class Query:\n"
            b"    def search(self, value=None):\n"
            b"        return value\n"
        )
        invocation = {
            "mode": "instance_method",
            "class_name": "Query",
            "method_name": "search",
            "constructor_args": [],
            "constructor_kwargs": {},
            "method_args": [],
            "method_kwargs": {},
        }

        mixed_cli = _request(script)
        mixed_cli["invocation"] = dict(invocation)
        response = server._run_skill_script(mixed_cli)
        self.assertEqual("invalid_function_call", response["error_code"])

        overflow = _request(script)
        overflow["argv"] = []
        overflow["invocation"] = dict(invocation)
        overflow["invocation"]["constructor_args"] = [None] * server.MAX_FUNCTION_ARGS
        overflow["invocation"]["method_args"] = [None]
        response = server._run_skill_script(overflow)
        self.assertEqual("invalid_function_call", response["error_code"])

        mixed_modes = _request(script)
        mixed_modes["argv"] = []
        mixed_modes["invocation"] = {**invocation, "name": "search"}
        response = server._run_skill_script(mixed_modes)
        self.assertEqual("invalid_invocation", response["error_code"])

    def test_timeout_kills_the_interpreter_process_group(self) -> None:
        script = b"sleep 30 &\nwait\n"
        request = _request(script, entrypoint="scripts/task.sh")
        request["timeout"] = 1

        response = server._run_skill_script(request)

        self.assertEqual("timeout", response["status"])
        self.assertEqual("script_timeout", response["error_code"])
        self.assertLess(response["duration_seconds"], 6)


class ResourceLimitConfigurationTests(unittest.TestCase):
    def test_trusted_browser_nproc_override_is_preserved_but_base_defaults_low(self) -> None:
        with patch.object(
            server,
            "_trusted_resource_launcher",
            return_value="/usr/bin/prlimit",
        ), patch.dict(
            os.environ,
            {"EXECUTOR_PROCESS_MAX_NPROC": "448"},
        ):
            browser = server._resource_limited_command(
                ["/bin/true"],
                cpu_seconds=60,
                persistent=True,
            )
        self.assertIn("--nproc=448:448", browser)

        with patch.object(
            server,
            "_trusted_resource_launcher",
            return_value="/usr/bin/prlimit",
        ), patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            base = server._resource_limited_command(
                ["/bin/true"],
                cpu_seconds=60,
                persistent=True,
            )
        self.assertIn("--nproc=64:64", base)

    def test_only_persistent_browser_profile_may_disable_sparse_as_limit(self) -> None:
        with patch.object(
            server,
            "_trusted_resource_launcher",
            return_value="/usr/bin/prlimit",
        ), patch.object(
            server,
            "MAX_ADDRESS_SPACE_BYTES",
            None,
        ), patch.dict(
            os.environ,
            {"EXECUTOR_RUNTIME_PROFILE": "browser-automation-v1"},
        ):
            browser = server._resource_limited_command(
                ["/bin/true"],
                cpu_seconds=60,
                persistent=True,
            )
        self.assertIn("--as=unlimited:unlimited", browser)

        with patch.object(
            server,
            "_trusted_resource_launcher",
            return_value="/usr/bin/prlimit",
        ), patch.object(
            server,
            "MAX_ADDRESS_SPACE_BYTES",
            None,
        ), patch.dict(
            os.environ,
            {"EXECUTOR_RUNTIME_PROFILE": "base-v1"},
        ):
            with self.assertRaises(server.ProtocolError) as caught:
                server._resource_limited_command(
                    ["/bin/true"],
                    cpu_seconds=60,
                    persistent=True,
                )
        self.assertEqual("invalid_resource_limit", caught.exception.code)


class PersistentProcessServerTests(unittest.TestCase):
    def setUp(self) -> None:
        server._shutdown_all_process_leases()
        worker_uid = 65532 if os.geteuid() != 65532 else 65531
        worker_gid = 65532 if os.getegid() != 65532 else 65531
        self.environment = patch.dict(
            os.environ,
            {
                "EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN,
                "EXECUTOR_ALLOWED_REQUEST_KINDS": "runtime_capabilities,process_lease",
                "EXECUTOR_WORKER_UID": str(worker_uid),
                "EXECUTOR_WORKER_GID": str(worker_gid),
            },
        )
        self.environment.start()
        self.worker_tree = patch.object(server, "_prepare_worker_tree", return_value=None)
        self.worker_tree.start()
        self.worker_spawn = patch.object(
            server,
            "_native_worker_popen_kwargs",
            return_value={"start_new_session": True},
        )
        self.worker_spawn.start()
        self.resource_limits = patch.object(
            server,
            "_resource_limited_command",
            side_effect=lambda command, **_: command,
        )
        self.resource_limits.start()
        self.worker_sweep = patch.object(
            server,
            "_sweep_configured_worker_uid",
            return_value=None,
        )
        self.worker_sweep_mock = self.worker_sweep.start()

    def tearDown(self) -> None:
        server._shutdown_all_process_leases()
        self.worker_sweep.stop()
        self.resource_limits.stop()
        self.worker_spawn.stop()
        self.worker_tree.stop()
        self.environment.stop()

    def test_v2_requires_hmac_and_safe_socket_mode(self) -> None:
        request = _process_open_request(b"print('must not run')\n")
        request["auth_hmac"] = "0" * 64
        response = server._run(request)
        self.assertEqual("v2_auth_failed", response["error_code"])
        self.assertEqual({}, server._PROCESS_LEASES)

        request = _process_open_request(b"print('must not run')\n")
        with patch.dict(os.environ, {"EXECUTOR_V2_AUTH_TOKEN": ""}):
            response = server._run(request)
        self.assertEqual("v2_auth_unavailable", response["error_code"])
        self.assertNotIn(V2_AUTH_TOKEN, json.dumps(response))

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXECUTOR_SOCKET_MODE", None)
            self.assertEqual(0o600, server._configured_socket_mode())
        with patch.dict(os.environ, {"EXECUTOR_SOCKET_MODE": "0660"}):
            self.assertEqual(0o660, server._configured_socket_mode())
        with patch.dict(os.environ, {"EXECUTOR_SOCKET_MODE": "0666"}):
            with self.assertRaises(RuntimeError):
                server._configured_socket_mode()

        fake_libc = type("FakeLibC", (), {"prctl": staticmethod(lambda *args: 0)})()
        with patch.object(server.ctypes, "CDLL", return_value=fake_libc):
            self.assertTrue(server._harden_daemon_dumpability())

    def test_authenticated_startup_reap_removes_orphan_lease_and_is_idempotent(self) -> None:
        opened = server._run(
            _process_open_request(b"print('orphan snapshot')\n")
        )
        self.assertEqual("success", opened["status"])
        handle = str(opened["lease_handle"])
        temp_dir = server._PROCESS_LEASES[handle].temp_dir
        self.assertTrue(temp_dir.is_dir())

        request = _sign_v2({
            "protocol_version": server.PROCESS_PROTOCOL_VERSION,
            "kind": "process_lease",
            "operation": "reap_all",
            "request_id": str(uuid.uuid4()),
            "op_id": str(uuid.uuid4()),
        })
        reaped = server._run(request)
        self.assertEqual("success", reaped["status"])
        self.assertEqual(1, reaped["reaped_leases"])
        self.assertIs(reaped["worker_processes_empty"], True)
        self.assertEqual({}, server._PROCESS_LEASES)
        self.assertIsNone(server._ACTIVE_PROCESS_LEASE_HANDLE)
        self.assertFalse(temp_dir.exists())

        replay = dict(request)
        replay["request_id"] = str(uuid.uuid4())
        replay["op_id"] = str(uuid.uuid4())
        replay = _sign_v2(replay)
        reaped_again = server._run(replay)
        self.assertEqual("success", reaped_again["status"])
        self.assertEqual(0, reaped_again["reaped_leases"])

        malformed = dict(replay)
        malformed["owner_scope"] = _owner_scope()
        malformed = _sign_v2(malformed)
        rejected = server._run(malformed)
        self.assertEqual(
            "invalid_process_request",
            rejected["error_code"],
        )

    def test_browser_profile_wraps_cli_and_persistent_object_entrypoints(self) -> None:
        wrapper = patch.object(
            server,
            "_browser_runtime_command",
            side_effect=lambda script, arguments: [
                "/fixed/browser-wrapper",
                str(script),
                *arguments,
            ],
        )
        with patch.dict(
            os.environ,
            {"EXECUTOR_RUNTIME_PROFILE": "browser-automation-v1"},
        ), wrapper as wrapped:
            cli_opened = server._run(
                _process_open_request(
                    b"print('browser cli')\n",
                )
            )
            self.assertEqual("success", cli_opened["status"])
            cli_lease = server._PROCESS_LEASES[
                str(cli_opened["lease_handle"])
            ]
            self.assertEqual(
                "/fixed/browser-wrapper",
                cli_lease.command[0],
            )
            self.assertTrue(
                cli_lease.command[1].endswith("/runtime/cli_runner.py")
            )
            prepared = server._run(_process_operation(cli_opened, "close"))
            _ack_process_batch(cli_opened, prepared)

            object_opened = server._run(
                _process_open_request(
                    (
                        b"class BrowserSession:\n"
                        b"    def snapshot(self):\n"
                        b"        return 'ok'\n"
                    ),
                    invocation={
                        "mode": "instance",
                        "class_name": "BrowserSession",
                        "constructor_args": [],
                        "constructor_kwargs": {},
                    },
                )
            )
            self.assertEqual("success", object_opened["status"])
            object_lease = server._PROCESS_LEASES[
                str(object_opened["lease_handle"])
            ]
            self.assertEqual(
                "/fixed/browser-wrapper",
                object_lease.command[0],
            )
            self.assertTrue(
                object_lease.command[1].endswith(
                    "/runtime/persistent_instance_runner.py"
                )
            )
            prepared = server._run(
                _process_operation(object_opened, "close")
            )
            _ack_process_batch(object_opened, prepared)

        self.assertGreaterEqual(wrapped.call_count, 2)

    def test_persistent_lease_owns_the_single_v1_v2_worker_slot_until_close_ack(self) -> None:
        with patch.dict(
            os.environ,
            {
                "EXECUTOR_ALLOWED_REQUEST_KINDS": (
                    "runtime_capabilities,process_lease,legacy_code"
                ),
            },
        ):
            opened = server._run(_process_open_request(b"print('not started')\n"))
            blocked = server._run({"code": "print('must not run')"})
            self.assertEqual("worker_busy", blocked["error_code"])

            prepared = server._run(_process_operation(opened, "close"))
            still_blocked = server._run({"code": "print('must not run')"})
            self.assertEqual("worker_busy", still_blocked["error_code"])

            closed = _ack_process_batch(opened, prepared)
            self.assertEqual("closed", closed["state"])
            allowed = server._run({"code": "print('after ack')"})
            self.assertEqual("success", allowed["status"])
            self.assertEqual("after ack", allowed["output"].strip())

    def test_ndjson_three_rounds_scope_binding_idempotency_sync_and_close(self) -> None:
        script = b"""
import json
import os
from pathlib import Path
import sys

count = 0
workspace = Path(os.environ["CHATDS_WORKSPACE"])
for line in sys.stdin:
    count += 1
    request = json.loads(line)
    (workspace / "result.txt").write_text(str(count), encoding="utf-8")
    print(json.dumps({"count": count, "value": request["value"]}), flush=True)
"""
        opened = server._run(_process_open_request(script))
        self.assertEqual("success", opened["status"])
        handle = str(opened["lease_handle"])
        temp_dir = server._PROCESS_LEASES[handle].temp_dir

        started = server._run(_process_operation(opened, "start"))
        self.assertEqual("running", started["state"])

        first_op = str(uuid.uuid4())
        first_write = _process_operation(
            opened,
            "write",
            op_id=first_op,
            stdin_b64=base64.b64encode(b'{"value":"one"}\n').decode("ascii"),
        )
        response = server._run(first_write)
        self.assertEqual(len(b'{"value":"one"}\n'), response["bytes_written"])
        replay = dict(first_write)
        replay["request_id"] = str(uuid.uuid4())
        replay = _sign_v2(replay)
        repeated = server._run(replay)
        self.assertTrue(repeated["idempotent_replay"])
        self.assertEqual(response["stdin_total_bytes"], repeated["stdin_total_bytes"])

        offset = 0
        observed: list[dict[str, object]] = []
        for index, value in enumerate(("one", "two", "three")):
            if index:
                payload = _process_operation(
                    opened,
                    "write",
                    stdin_b64=base64.b64encode(
                        json.dumps({"value": value}).encode("utf-8") + b"\n"
                    ).decode("ascii"),
                )
                self.assertEqual("success", server._run(payload)["status"])
            read = server._run(_process_operation(
                opened,
                "read",
                stdout_offset=offset,
                stderr_offset=0,
                max_bytes=64 * 1024,
                wait_ms=1_000,
            ))
            chunk = base64.b64decode(read["stdout_b64"])
            observed.extend(
                json.loads(line)
                for line in chunk.decode("utf-8").splitlines()
                if line
            )
            offset = read["stdout_next_offset"]

        self.assertEqual([1, 2, 3], [item["count"] for item in observed])
        self.assertEqual(["one", "two", "three"], [item["value"] for item in observed])

        wrong_scope = _owner_scope(user_id="different-user")
        rejected = server._run(_process_operation(
            opened,
            "read",
            owner_scope=wrong_scope,
            stdout_offset=offset,
            stderr_offset=0,
            max_bytes=1024,
            wait_ms=0,
        ))
        self.assertEqual("lease_scope_mismatch", rejected["error_code"])

        sync_op = str(uuid.uuid4())
        sync_request = _process_operation(opened, "sync", op_id=sync_op)
        synced = server._run(sync_request)
        self.assertEqual("success", synced["status"])
        self.assertEqual(
            b"3",
            base64.b64decode(synced["artifacts"][0]["content_b64"]),
        )
        sync_replay = dict(sync_request)
        sync_replay["request_id"] = str(uuid.uuid4())
        sync_replay = _sign_v2(sync_replay)
        repeated_sync = server._run(sync_replay)
        self.assertTrue(repeated_sync["idempotent_replay"])
        self.assertEqual(synced["artifacts"], repeated_sync["artifacts"])
        acknowledged_sync = _ack_process_batch(opened, synced)
        self.assertTrue(acknowledged_sync["sync_acknowledged"])
        self.assertEqual("sync", acknowledged_sync["acknowledged_operation"])

        close_op = str(uuid.uuid4())
        close_request = _process_operation(opened, "close", op_id=close_op)
        prepared_close = server._run(close_request)
        self.assertEqual("closing", prepared_close["state"])
        self.assertTrue(temp_dir.exists())
        closed = _ack_process_batch(opened, prepared_close)
        self.assertEqual("closed", closed["state"])
        self.assertFalse(temp_dir.exists())
        replay_close = dict(close_request)
        replay_close["request_id"] = str(uuid.uuid4())
        replay_close = _sign_v2(replay_close)
        repeated_close = server._run(replay_close)
        self.assertTrue(repeated_close["idempotent_replay"])

    def test_persistent_commonjs_entrypoint(self) -> None:
        script = b"""
"use strict";
const readline = require("readline");
let count = 0;
const input = readline.createInterface({input: process.stdin});
input.on("line", line => {
  count += 1;
  console.log(JSON.stringify({count, value: JSON.parse(line).value}));
});
"""
        opened = server._run(_process_open_request(
            script,
            entrypoint="scripts/task.cjs",
        ))
        self.assertEqual("node", opened["interpreter"])
        self.assertEqual(
            "success",
            server._run(_process_operation(opened, "start"))["status"],
        )
        self.assertEqual(
            "success",
            server._run(_process_operation(
                opened,
                "write",
                stdin_b64=base64.b64encode(b'{"value":"cjs"}\n').decode("ascii"),
            ))["status"],
        )
        read = server._run(_process_operation(
            opened,
            "read",
            stdout_offset=0,
            stderr_offset=0,
            max_bytes=4096,
            wait_ms=1_000,
        ))
        self.assertEqual(
            {"count": 1, "value": "cjs"},
            json.loads(base64.b64decode(read["stdout_b64"])),
        )
        prepared = server._run(_process_operation(opened, "close"))
        _ack_process_batch(opened, prepared)

    def test_stdin_close_delivers_eof_for_cli_then_read_sync_and_close_continue(self) -> None:
        script = b"""
import os
from pathlib import Path
import sys

content = sys.stdin.read()
Path(os.environ["CHATDS_WORKSPACE"]).joinpath("eof.txt").write_text(content)
print("eof-received", flush=True)
"""
        opened = server._run(_process_open_request(script))
        server._run(_process_operation(opened, "start"))
        written = server._run(_process_operation(
            opened,
            "write",
            stdin_b64=base64.b64encode(b"payload").decode("ascii"),
        ))
        self.assertEqual("success", written["status"])
        eof = server._run(_process_operation(opened, "stdin_close"))
        self.assertIs(eof["stdin_closed"], True)
        self.assertIs(eof["already_closed"], False)
        repeated_eof = server._run(_process_operation(opened, "stdin_close"))
        self.assertIs(repeated_eof["stdin_closed"], True)
        self.assertIs(repeated_eof["already_closed"], True)

        rejected_write = server._run(_process_operation(
            opened,
            "write",
            stdin_b64=base64.b64encode(b"late").decode("ascii"),
        ))
        self.assertEqual("stdin_closed", rejected_write["error_code"])
        read = server._run(_process_operation(
            opened,
            "read",
            stdout_offset=0,
            stderr_offset=0,
            max_bytes=4096,
            wait_ms=server.MAX_PROCESS_READ_WAIT_MS,
        ))
        self.assertIn(b"eof-received", base64.b64decode(read["stdout_b64"]))
        synced = server._run(_process_operation(opened, "sync"))
        self.assertEqual(
            b"payload",
            base64.b64decode(synced["artifacts"][0]["content_b64"]),
        )
        _ack_process_batch(opened, synced)
        prepared = server._run(_process_operation(opened, "close"))
        self.assertEqual(
            "closed",
            _ack_process_batch(opened, prepared)["state"],
        )

    def test_public_instance_calls_preserve_state_and_reject_private_method(self) -> None:
        script = b"""
class Counter:
    def __init__(self, start=0):
        self.value = start

    def add(self, amount=1):
        self.value += amount
        return self.value

    def _private(self):
        return 999
"""
        opened = server._run(_process_open_request(
            script,
            invocation={
                "mode": "instance",
                "class_name": "Counter",
                "constructor_args": [5],
                "constructor_kwargs": {},
            },
        ))
        self.assertEqual("instance", opened["invocation_mode"])
        server._run(_process_operation(opened, "start"))

        ready = server._run(_process_operation(
            opened,
            "read",
            stdout_offset=0,
            stderr_offset=0,
            max_bytes=4096,
            wait_ms=1_000,
        ))
        offset = ready["stdout_next_offset"]
        self.assertEqual(
            "ready",
            json.loads(base64.b64decode(ready["stdout_b64"]))["event"],
        )

        results: list[int] = []
        for amount in (2, 3):
            called = server._run(_process_operation(
                opened,
                "call",
                method_name="add",
                method_args=[amount],
                method_kwargs={},
            ))
            self.assertEqual("success", called["status"])
            read = server._run(_process_operation(
                opened,
                "read",
                stdout_offset=offset,
                stderr_offset=0,
                max_bytes=4096,
                wait_ms=1_000,
            ))
            envelope = json.loads(base64.b64decode(read["stdout_b64"]))
            results.append(envelope["result"])
            offset = read["stdout_next_offset"]
        self.assertEqual([7, 10], results)

        rejected = server._run(_process_operation(
            opened,
            "call",
            method_name="_private",
            method_args=[],
            method_kwargs={},
        ))
        self.assertEqual("invalid_function_call", rejected["error_code"])
        prepared = server._run(_process_operation(opened, "close"))
        _ack_process_batch(opened, prepared)

    def test_exact_public_factory_returns_stateful_object_for_repeated_calls(self) -> None:
        script = b"""
import asyncio

class Counter:
    def __init__(self, start=0):
        self.value = start
        self.event_loop = asyncio.get_running_loop()

    def add(self, amount=1):
        self.value += amount
        return self.value

    async def uses_factory_event_loop(self):
        return self.event_loop is asyncio.get_running_loop()

async def open_counter(start=0):
    return Counter(start)
"""
        opened = server._run(_process_open_request(
            script,
            invocation={
                "mode": "factory",
                "factory_name": "open_counter",
                "factory_args": [10],
                "factory_kwargs": {},
            },
        ))
        self.assertEqual("success", opened["status"])
        self.assertEqual("factory", opened["invocation_mode"])
        self.assertEqual("open_counter", opened["factory_name"])
        server._run(_process_operation(opened, "start"))
        ready = server._run(_process_operation(
            opened,
            "read",
            stdout_offset=0,
            stderr_offset=0,
            max_bytes=4096,
            wait_ms=1_000,
        ))
        ready_event = json.loads(base64.b64decode(ready["stdout_b64"]))
        self.assertEqual("factory", ready_event["invocation_mode"])
        self.assertEqual("open_counter", ready_event["factory_name"])
        offset = ready["stdout_next_offset"]

        server._run(_process_operation(
            opened,
            "call",
            method_name="uses_factory_event_loop",
            method_args=[],
            method_kwargs={},
        ))
        loop_read = server._run(_process_operation(
            opened,
            "read",
            stdout_offset=offset,
            stderr_offset=0,
            max_bytes=4096,
            wait_ms=1_000,
        ))
        loop_result = json.loads(base64.b64decode(loop_read["stdout_b64"]))
        self.assertIs(loop_result["result"], True)
        offset = loop_read["stdout_next_offset"]

        values: list[int] = []
        for amount in (4, 6):
            called = server._run(_process_operation(
                opened,
                "call",
                method_name="add",
                method_args=[amount],
                method_kwargs={},
            ))
            self.assertEqual("success", called["status"])
            read = server._run(_process_operation(
                opened,
                "read",
                stdout_offset=offset,
                stderr_offset=0,
                max_bytes=4096,
                wait_ms=1_000,
            ))
            values.append(json.loads(base64.b64decode(read["stdout_b64"]))["result"])
            offset = read["stdout_next_offset"]
        self.assertEqual([14, 20], values)
        prepared = server._run(_process_operation(opened, "close"))
        _ack_process_batch(opened, prepared)

    def test_idle_ttl_expires_and_cleans_process_directory(self) -> None:
        opened = server._run(_process_open_request(
            b"import time\ntime.sleep(60)\n",
            idle_ttl_seconds=1,
        ))
        handle = str(opened["lease_handle"])
        lease = server._PROCESS_LEASES[handle]
        temp_dir = lease.temp_dir
        lease.last_activity = time.monotonic() - 5

        self.assertEqual(1, server._cleanup_expired_process_leases())
        self.assertEqual("expired", lease.state)
        self.assertFalse(temp_dir.exists())
        expired = server._run(_process_operation(
            opened,
            "read",
            stdout_offset=0,
            stderr_offset=0,
            max_bytes=1024,
            wait_ms=0,
        ))
        self.assertEqual("lease_expired", expired["error_code"])

        server._cleanup_expired_process_leases(
            now=float(lease.closed_at) + server.PROCESS_TOMBSTONE_SECONDS + 1,
        )
        self.assertNotIn(handle, server._PROCESS_LEASES)

    def test_failed_expiry_containment_is_quarantined_until_retry_succeeds(self) -> None:
        opened = server._run(_process_open_request(
            b"print('never started')\n",
            idle_ttl_seconds=1,
        ))
        handle = str(opened["lease_handle"])
        lease = server._PROCESS_LEASES[handle]
        temp_dir = lease.temp_dir
        lease.last_activity = time.monotonic() - 5
        self.worker_sweep_mock.side_effect = [
            server.ProtocolError(
                "worker_containment_failed",
                "injected containment failure",
            ),
            None,
            None,
        ]
        try:
            self.assertEqual(0, server._cleanup_expired_process_leases())
            self.assertEqual("quarantined", lease.state)
            self.assertIsNone(lease.closed_at)
            self.assertTrue(temp_dir.exists())
            self.assertEqual(handle, server._ACTIVE_PROCESS_LEASE_HANDLE)

            self.assertEqual(1, server._cleanup_expired_process_leases())
            self.assertEqual("expired", lease.state)
            self.assertFalse(temp_dir.exists())
            self.assertIsNotNone(lease.closed_at)
            self.assertIsNone(server._ACTIVE_PROCESS_LEASE_HANDLE)
        finally:
            self.worker_sweep_mock.side_effect = None

    def test_close_ack_retries_same_batch_when_tree_cleanup_initially_fails(self) -> None:
        opened = server._run(_process_open_request(b"print('never started')\n"))
        handle = str(opened["lease_handle"])
        lease = server._PROCESS_LEASES[handle]
        temp_dir = lease.temp_dir
        original_remove = server._remove_process_tree
        calls = 0

        def flaky_remove(path: Path) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                return False
            return original_remove(path)

        with patch.object(server, "_remove_process_tree", side_effect=flaky_remove):
            prepared = server._run(_process_operation(opened, "close"))
            ack_request = _process_operation(
                opened,
                "ack",
                sync_token=prepared["sync_token"],
            )
            failed = server._run(ack_request)
            self.assertEqual("worker_containment_failed", failed["error_code"])
            self.assertEqual("closing", lease.state)
            self.assertTrue(temp_dir.exists())
            closed = server._run(ack_request)
            self.assertEqual("closed", closed["state"])
            server._cleanup_expired_process_leases(
                now=float(lease.closed_at) + server.PROCESS_TOMBSTONE_SECONDS + 1,
            )

        self.assertGreaterEqual(calls, 2)
        self.assertFalse(temp_dir.exists())
        self.assertNotIn(handle, server._PROCESS_LEASES)

    def test_close_kills_descendants_after_entrypoint_leader_exits(self) -> None:
        script = b"""
import subprocess
child = subprocess.Popen(["/bin/sleep", "60"])
print(child.pid, flush=True)
"""
        opened = server._run(_process_open_request(script))
        server._run(_process_operation(opened, "start"))
        read = server._run(_process_operation(
            opened,
            "read",
            stdout_offset=0,
            stderr_offset=0,
            max_bytes=4096,
            wait_ms=1_000,
        ))
        child_pid = int(base64.b64decode(read["stdout_b64"]).strip())
        # The entrypoint leader has exited, but its child inherited the same
        # process group and still owns the stdout/stderr descriptors.
        lease = server._PROCESS_LEASES[str(opened["lease_handle"])]
        for _ in range(50):
            if lease.process is not None and lease.process.poll() is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(lease.process)
        self.assertIsNotNone(lease.process.poll())

        prepared = server._run(_process_operation(opened, "close"))
        closed = _ack_process_batch(opened, prepared)
        self.assertEqual("closed", closed["state"])
        for _ in range(50):
            proc_stat = Path(f"/proc/{child_pid}/stat")
            if not proc_stat.exists():
                break
            try:
                if proc_stat.read_text(encoding="utf-8").split()[2] == "Z":
                    break
            except (OSError, IndexError):
                break
            time.sleep(0.01)
        else:
            self.fail("orphaned process-group child survived lease close")

    def test_browser_profile_forwards_only_fixed_runtime_environment_and_policy_receipt(self) -> None:
        profile_environment = {
            "EXECUTOR_RUNTIME_PROFILE": "browser-automation-v1",
            "EXECUTOR_ALLOWED_REQUEST_KINDS": "runtime_capabilities,process_lease",
            "EXECUTOR_MAX_PROCESS_LEASES": "1",
            "EXECUTOR_MAX_PROCESS_LEASES_PER_SCOPE": "1",
            "SKILL_EGRESS_PROXY_URL": "http://skill-egress-proxy:8080",
            "NODE_PATH": "/opt/node",
            "PLAYWRIGHT_BROWSERS_PATH": "/opt/browsers",
            "BROWSER_EXECUTABLE": "/usr/bin/chromium",
            "CHROME_BIN": "/usr/bin/chromium",
            "SE_OFFLINE": "true",
            "SE_AVOID_STATS": "true",
            "SE_AVOID_BROWSER_DOWNLOAD": "true",
            "SE_SECRET": "must-not-cross",
            "UNRELATED_SECRET": "must-not-cross",
        }
        with patch.dict(os.environ, profile_environment), patch.object(
            server,
            "_browser_runtime_command",
            side_effect=lambda script, arguments: [
                "/fixed/browser-wrapper",
                str(script),
                *arguments,
            ],
        ):
            capabilities = server._run({
                "protocol_version": server.PROTOCOL_VERSION,
                "kind": "runtime_capabilities",
                "request_id": str(uuid.uuid4()),
                "requirements": [],
                "commands": [],
                "environment_variables": [
                    "DISPLAY",
                    "WAYLAND_DISPLAY",
                    "XDG_RUNTIME_DIR",
                    "NODE_PATH",
                    "PLAYWRIGHT_BROWSERS_PATH",
                    "BROWSER_EXECUTABLE",
                    "CHROME_BIN",
                    "SE_OFFLINE",
                    "SE_AVOID_STATS",
                    "SE_AVOID_BROWSER_DOWNLOAD",
                    "SKILL_EGRESS_PROXY_URL",
                    "SE_SECRET",
                ],
                "platform_groups": [],
            })
            self.assertFalse(capabilities["valid"])
            availability = {
                item["name"]: item["available"]
                for item in capabilities["environment_variables"]
            }
            self.assertTrue(all(
                availability[name]
                for name in (
                    "WAYLAND_DISPLAY",
                    "XDG_RUNTIME_DIR",
                    "NODE_PATH",
                    "PLAYWRIGHT_BROWSERS_PATH",
                    "BROWSER_EXECUTABLE",
                    "CHROME_BIN",
                    "SE_OFFLINE",
                    "SE_AVOID_STATS",
                    "SE_AVOID_BROWSER_DOWNLOAD",
                    "SKILL_EGRESS_PROXY_URL",
                )
            ))
            self.assertFalse(availability["DISPLAY"])
            self.assertFalse(availability["SE_SECRET"])
            self.assertEqual(
                {"direct": "disabled", "egress": "policy_proxy"},
                capabilities["runtime_identity"]["network_policy"],
            )
            self.assertEqual(
                "wayland-headless",
                capabilities["runtime_identity"]["display_backend"],
            )
            self.assertIs(
                True,
                capabilities["runtime_identity"]["headed_browser"],
            )
            self.assertIs(False, capabilities["runtime_identity"]["x11"])
            opened = server._run(_process_open_request(b"import time\ntime.sleep(60)\n"))
            self.assertEqual("success", opened["status"])
            self.assertEqual("browser-automation-v1", opened["runtime_profile"])
            self.assertEqual(
                {"direct": "disabled", "egress": "policy_proxy"},
                opened["network_policy"],
            )
            lease = server._PROCESS_LEASES[str(opened["lease_handle"])]
            self.assertNotIn("DISPLAY", lease.environment)
            self.assertNotIn("WAYLAND_DISPLAY", lease.environment)
            self.assertEqual(
                "http://skill-egress-proxy:8080",
                lease.environment["HTTPS_PROXY"],
            )
            self.assertEqual(
                "localhost,127.0.0.1,[::1]",
                lease.environment["NO_PROXY"],
            )
            self.assertNotIn("EXECUTOR_V2_AUTH_TOKEN", lease.environment)
            self.assertNotIn("UNRELATED_SECRET", lease.environment)
            self.assertNotIn("SE_SECRET", lease.environment)
            rendered = json.dumps(opened)
            self.assertNotIn(V2_AUTH_TOKEN, rendered)
            self.assertNotIn("must-not-cross", rendered)
            self.assertNotIn("http://skill-egress-proxy:8080", rendered)

            second = server._run(_process_open_request(b"print('quota')\n"))
            self.assertEqual("lease_quota_exceeded", second["error_code"])

            blocked = server._run(_request(b"print('blocked')\n"))
            self.assertEqual("request_kind_disabled", blocked["error_code"])


class WorkerContainmentTests(unittest.TestCase):
    def test_healthcheck_remains_healthy_during_active_worker_state(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.response = b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def settimeout(self, _timeout):
                return None

            def connect(self, _path):
                return None

            def sendall(self, request: bytes):
                payload = json.loads(request.decode("utf-8"))
                response = server._run_runtime_capabilities(payload)
                self.response = (
                    json.dumps(response, separators=(",", ":")).encode("utf-8")
                    + b"\n"
                )

            def recv(self, _size: int) -> bytes:
                response, self.response = self.response, b""
                return response

        with patch.object(
            server,
            "_trusted_resource_launcher",
            return_value="/usr/bin/prlimit",
        ), patch.object(
            server,
            "_untrusted_execution_enabled",
            return_value=True,
        ), patch.object(
            server,
            "_validate_worker_controller_security",
        ), patch.object(
            server,
            "_configured_worker_identity",
            return_value=(65_528, 65_528),
        ), patch.object(
            server,
            "_validate_worker_shared_state_roots",
        ) as root_boundary, patch.object(
            server,
            "_worker_owned_shared_entries",
            side_effect=AssertionError(
                "active lease descendants must not be audited by healthcheck"
            ),
        ) as residue_audit, patch.object(
            server,
            "_process_protocol_enabled",
            return_value=False,
        ), patch.object(
            server.socket,
            "socket",
            return_value=FakeSocket(),
        ), patch.dict(
            os.environ,
            {
                "EXECUTOR_RUNTIME_PROFILE": "base-v1",
                "EXECUTOR_WORKER_UID": "65528",
                "EXECUTOR_WORKER_GID": "65528",
                "EXECUTOR_ENFORCE_SHARED_STATE_ISOLATION": "1",
            },
        ):
            self.assertEqual(0, server.healthcheck())
        root_boundary.assert_called_once_with(
            worker_uid=65_528,
            worker_gid=65_528,
        )
        residue_audit.assert_not_called()

    def test_shared_roots_reject_worker_writable_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "shared"
            root.mkdir(mode=0o777)
            root.chmod(0o777)
            with patch.object(
                server,
                "_worker_shared_state_roots",
                return_value=(root,),
            ):
                with self.assertRaises(server.ProtocolError) as caught:
                    server._worker_owned_shared_entries(
                        worker_uid=os.getuid() + 10_000,
                        worker_gid=os.getgid() + 10_000,
                    )
        self.assertEqual(
            "worker_shared_state_unavailable",
            caught.exception.code,
        )

    def test_shared_roots_reject_mutable_file_owned_by_another_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "shared"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            mutable = root / "cross-lease"
            mutable.write_text("state", encoding="utf-8")
            mutable.chmod(0o666)
            with patch.object(
                server,
                "_worker_shared_state_roots",
                return_value=(root,),
            ):
                with self.assertRaises(server.ProtocolError) as caught:
                    server._worker_owned_shared_entries(
                        worker_uid=os.getuid() + 10_000,
                        worker_gid=os.getgid() + 10_000,
                    )
        self.assertEqual(
            "worker_shared_state_unavailable",
            caught.exception.code,
        )

    def test_shared_roots_reject_persistent_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "shared"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            (root / "redirect").symlink_to("/tmp")
            with patch.object(
                server,
                "_worker_shared_state_roots",
                return_value=(root,),
            ):
                with self.assertRaises(server.ProtocolError) as caught:
                    server._worker_owned_shared_entries(
                        worker_uid=os.getuid() + 10_000,
                        worker_gid=os.getgid() + 10_000,
                    )
        self.assertEqual(
            "worker_shared_state_unavailable",
            caught.exception.code,
        )

    def test_shared_state_cleanup_removes_sentinel_before_next_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "cross-lease-sentinel"
            sentinel.write_text("previous lease", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "EXECUTOR_WORKER_UID": str(os.getuid()),
                    "EXECUTOR_WORKER_GID": str(os.getgid()),
                    "EXECUTOR_ENFORCE_SHARED_STATE_ISOLATION": "1",
                },
            ), patch.object(
                server,
                "_worker_owned_shared_entries",
                side_effect=[[sentinel], [], []],
            ):
                server._purge_configured_worker_shared_state()
            self.assertFalse(sentinel.exists())

    def test_uid_sweep_stops_then_kills_and_requires_two_empty_scans(self) -> None:
        scans = [
            {101, 102},
            {101, 102, 103},
            {101, 102, 103},
            {101, 102, 103},
            set(),
            set(),
        ]
        with patch.dict(
            os.environ,
            {
                "EXECUTOR_WORKER_UID": "60001",
                "EXECUTOR_WORKER_GID": "60001",
            },
        ), patch.object(
            server,
            "_worker_uid_processes",
            side_effect=scans,
        ) as scan, patch.object(
            server.os,
            "kill",
        ) as kill, patch.object(
            server,
            "_reap_worker_children",
        ), patch.object(
            server.time,
            "sleep",
        ):
            server._sweep_configured_worker_uid()

        self.assertEqual(6, scan.call_count)
        stopped = {
            call.args[0]
            for call in kill.call_args_list
            if call.args[1] == server.signal.SIGSTOP
        }
        killed = {
            call.args[0]
            for call in kill.call_args_list
            if call.args[1] == server.signal.SIGKILL
        }
        self.assertEqual({101, 102, 103}, stopped)
        self.assertEqual({101, 102, 103}, killed)

    @unittest.skipUnless(os.geteuid() == 0, "requires a root controller")
    def test_real_uid_sweep_contains_setsid_double_fork_and_sigterm_refork(self) -> None:
        worker_uid = 60001
        worker_gid = 60001
        try:
            server._validate_controller_capabilities()
        except server.ProtocolError as exc:
            self.skipTest(str(exc))
        if server._worker_uid_processes(worker_uid):
            self.skipTest("dedicated test worker UID is already in use")
        script = b"""
import os
import signal
import time

def refork(*_):
    child = os.fork()
    if child == 0:
        os.setsid()
        time.sleep(60)
        os._exit(0)
    os._exit(0)

signal.signal(signal.SIGTERM, refork)
first = os.fork()
if first == 0:
    os.setsid()
    second = os.fork()
    if second:
        os._exit(0)
    time.sleep(60)
    os._exit(0)
os.waitpid(first, 0)
print("ready", flush=True)
while True:
    time.sleep(1)
"""
        with patch.dict(
            os.environ,
            {
                "EXECUTOR_V2_AUTH_TOKEN": V2_AUTH_TOKEN,
                "EXECUTOR_ALLOWED_REQUEST_KINDS": (
                    "runtime_capabilities,process_lease"
                ),
                "EXECUTOR_WORKER_UID": str(worker_uid),
                "EXECUTOR_WORKER_GID": str(worker_gid),
            },
        ):
            server._shutdown_all_process_leases()
            try:
                opened = server._run(_process_open_request(script))
                self.assertEqual("success", opened["status"])
                self.assertEqual(
                    "success",
                    server._run(_process_operation(opened, "start"))["status"],
                )
                ready = server._run(_process_operation(
                    opened,
                    "read",
                    stdout_offset=0,
                    stderr_offset=0,
                    max_bytes=4096,
                    wait_ms=2_000,
                ))
                self.assertIn(
                    b"ready",
                    base64.b64decode(ready["stdout_b64"]),
                    ready,
                )
                prepared = server._run(_process_operation(opened, "close"))
                self.assertEqual(
                    "closed",
                    _ack_process_batch(opened, prepared)["state"],
                )
                self.assertEqual(set(), server._worker_uid_processes(worker_uid))

                second = server._run(_process_open_request(b"print('next')\n"))
                self.assertEqual("success", second["status"])
                second_close = server._run(_process_operation(second, "close"))
                self.assertEqual(
                    "closed",
                    _ack_process_batch(second, second_close)["state"],
                )
            finally:
                server._shutdown_all_process_leases()


if __name__ == "__main__":
    unittest.main()
