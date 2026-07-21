from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import sys
import unittest
import uuid
from unittest.mock import patch

from executor import server


def _file(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


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
                "environment_variables": ["CHATDS_WORKSPACE", "INTERNAL_API_TOKEN"],
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


if __name__ == "__main__":
    unittest.main()
