from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
import uuid

from tools import isolated_skill_executor as client


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
