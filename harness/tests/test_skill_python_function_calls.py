from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tools import skill_python
from tools.context import ToolContext
from tools.isolated_skill_executor import IsolatedSkillExecutorError


class SkillPythonFunctionCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.skills = self.root / "skills"
        self.sandboxes = self.root / "sandboxes"
        self.user_id = "function-user"
        self.session_id = "function-session"
        self.skill = self.skills / self.user_id / self.session_id / "database-helper"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(
            "---\nname: database-helper\ndescription: generic helper\n---\n",
            encoding="utf-8",
        )

        def fake_sandbox_dir(user_id: str, session_id: str, sub: str = "files") -> Path:
            path = (self.sandboxes / user_id / session_id / sub).resolve()
            path.mkdir(parents=True, exist_ok=True)
            return path

        self.isolated = AsyncMock(return_value={
            "protocol_version": 1,
            "kind": "skill_script_result",
            "status": "success",
            "returncode": 0,
            "network": "disabled",
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "artifacts": [],
            "workspace_applied": True,
        })
        self.patches = [
            patch.object(skill_python, "USER_SKILLS_BASE", self.skills),
            patch("tools.skill_script.skill_scanner.USER_SKILLS_BASE", self.skills),
            patch.object(skill_python, "sandbox_dir", side_effect=fake_sandbox_dir),
            patch.object(skill_python, "execute_isolated_skill_script", self.isolated),
            patch.object(
                skill_python.asyncio,
                "create_subprocess_exec",
                AsyncMock(side_effect=AssertionError("Skill code must not run in the harness")),
            ),
            patch.object(
                skill_python,
                "ensure_session_runtime",
                AsyncMock(side_effect=AssertionError("Skill runtime must not initialize in the harness")),
            ),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.tempdir.cleanup()

    def write_script(self, content: str, name: str = "helper.py") -> Path:
        script = self.skill / "scripts" / name
        script.write_text(content, encoding="utf-8")
        return script

    async def call(self, **kwargs):
        result = await skill_python.run_skill_python(
            user_id=self.user_id,
            session_id=self.session_id,
            **kwargs,
        )
        return json.loads(result)

    def test_tool_schema_exposes_declarative_function_mode(self) -> None:
        properties = skill_python.RUN_SKILL_PYTHON_SCHEMA["parameters"]["properties"]
        self.assertIn("function_name", properties)
        self.assertIn("function_args", properties)
        self.assertIn("function_kwargs", properties)
        self.assertIn("class_name", properties)
        self.assertIn("method_name", properties)
        self.assertIn("constructor_args", properties)
        self.assertIn("constructor_kwargs", properties)
        self.assertIn("method_args", properties)
        self.assertIn("method_kwargs", properties)
        self.assertIn("dotted/private", skill_python.RUN_SKILL_PYTHON_SCHEMA["description"])
        self.assertIn("mutually exclusive", skill_python.RUN_SKILL_PYTHON_SCHEMA["description"])

    def test_pure_preflight_rejects_unknown_function_before_executor(self) -> None:
        self.write_script("def search(query):\n    return {'query': query}\n")
        context = ToolContext(
            user_id=self.user_id,
            session_id=self.session_id,
        )

        denied = skill_python.preflight_run_skill_python_args(
            {
                "script_path": "skills/database-helper/scripts/helper.py",
                "function_name": "invented_writer",
                "function_args": ["value"],
            },
            context,
        )
        allowed = skill_python.preflight_run_skill_python_args(
            {
                "script_path": "skills/database-helper/scripts/helper.py",
                "function_name": "search",
                "function_args": ["value"],
            },
            context,
        )

        self.assertEqual(
            denied["reason"],
            "skill_python_invocation_preflight_failed",
        )
        self.assertIn("not declared", denied["error"])
        self.assertEqual(
            ["search"],
            [
                item["name"]
                for item in denied["available_functions"]
            ],
        )
        self.assertIsNone(allowed)
        self.isolated.assert_not_awaited()

    async def test_public_function_call_is_forwarded_as_data_to_exact_skill_snapshot(self) -> None:
        self.write_script(
            "def search(query, limit=2):\n"
            "    return {'query': query, 'records': list(range(limit))}\n"
        )
        self.isolated.return_value.update({
            "invocation_mode": "function",
            "function_name": "search",
            "result": {"query": "target", "records": [0, 1, 2]},
        })

        result = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            function_name="search",
            function_args=["target"],
            function_kwargs={"limit": 3},
        )

        self.assertEqual("success", result["status"])
        self.assertEqual("function", result["invocation_mode"])
        self.assertEqual({"query": "target", "records": [0, 1, 2]}, result["result"])
        kwargs = self.isolated.await_args.kwargs
        self.assertEqual(self.skill.resolve(), kwargs["skill_root"])
        self.assertEqual("scripts/helper.py", kwargs["entrypoint"])
        self.assertEqual("search", kwargs["function_name"])
        self.assertEqual(["target"], kwargs["function_args"])
        self.assertEqual({"limit": 3}, kwargs["function_kwargs"])
        self.assertEqual([], kwargs["args"])
        self.assertEqual("workspace", kwargs["cwd"])
        self.assertTrue(result["isolated_execution"])
        self.assertEqual("isolated_executor", result["runtime_status"])
        self.assertFalse(result["managed_fallback"])

    async def test_public_instance_method_is_forwarded_with_exact_identity(self) -> None:
        self.write_script(
            "class QueryClient:\n"
            "    def __init__(self, prefix, use_cache=False):\n"
            "        self.prefix = prefix\n"
            "        self.use_cache = use_cache\n"
            "    def search(self, query, limit=2):\n"
            "        return {'query': self.prefix + query, 'limit': limit}\n"
        )
        self.isolated.return_value.update({
            "invocation_mode": "instance_method",
            "class_name": "QueryClient",
            "method_name": "search",
            "result": {"query": "target", "limit": 3},
        })

        result = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            class_name="QueryClient",
            method_name="search",
            constructor_args=[""],
            constructor_kwargs={"use_cache": False},
            method_args=["target"],
            method_kwargs={"limit": 3},
        )

        self.assertEqual("success", result["status"])
        self.assertEqual("instance_method", result["invocation_mode"])
        self.assertEqual("QueryClient", result["class_name"])
        self.assertEqual("search", result["method_name"])
        kwargs = self.isolated.await_args.kwargs
        self.assertEqual("QueryClient", kwargs["class_name"])
        self.assertEqual("search", kwargs["method_name"])
        self.assertEqual([""], kwargs["constructor_args"])
        self.assertEqual({"use_cache": False}, kwargs["constructor_kwargs"])
        self.assertEqual(["target"], kwargs["method_args"])
        self.assertEqual({"limit": 3}, kwargs["method_kwargs"])
        self.assertIsNone(kwargs["function_name"])
        self.assertEqual([], kwargs["args"])

    async def test_cli_mode_uses_same_isolated_executor_without_managed_fallback(self) -> None:
        self.write_script("if __name__ == '__main__':\n    print('cli')\n")
        self.isolated.return_value["stdout"] = "cli:unchanged\n"

        result = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            args=["unchanged"],
            cwd="script",
        )

        self.assertEqual("success", result["status"])
        self.assertEqual("cli", result["invocation_mode"])
        self.assertIn("cli:unchanged", result["stdout"])
        kwargs = self.isolated.await_args.kwargs
        self.assertEqual(["unchanged"], kwargs["args"])
        self.assertIsNone(kwargs["function_name"])
        self.assertEqual("script", kwargs["cwd"])

    async def test_promoted_user_python_requires_runtime_enabled_whitelist(self) -> None:
        promoted = self.skills / self.user_id / "promoted-python"
        (promoted / "scripts").mkdir(parents=True)
        (promoted / "SKILL.md").write_text(
            "---\nname: promoted-python\ndescription: promoted python helper\n---\n",
            encoding="utf-8",
        )
        (promoted / "scripts" / "helper.py").write_text(
            "def normalize(value):\n    return value\n",
            encoding="utf-8",
        )

        denied = await self.call(
            script_path="skills/promoted-python/scripts/helper.py",
            function_name="normalize",
            function_args=[3],
        )
        allowed = await self.call(
            script_path="skills/promoted-python/scripts/helper.py",
            function_name="normalize",
            function_args=[3],
            enabled_user_skills=["promoted-python"],
        )

        self.assertEqual("error", denied["status"])
        self.assertEqual("success", allowed["status"])
        self.assertEqual(promoted.resolve(), self.isolated.await_args.kwargs["skill_root"])

    async def test_declared_dependency_preflight_blocks_function_before_dispatch(self) -> None:
        self.write_script("def search():\n    return 'must not run'\n")
        with patch.object(
            skill_python,
            "preflight_declared_skill_dependencies",
            return_value={
                "valid": False,
                "checked": True,
                "blockers": [{
                    "code": "unsatisfied_python_dependencies",
                    "items": [{
                        "requirement": "graph-addon>=9",
                        "status": "version_conflict",
                    }],
                }],
                "packages": {
                    "requirements": ["graph-addon>=9"],
                    "status": "unsatisfied",
                },
            },
        ):
            result = await self.call(
                script_path="skills/database-helper/scripts/helper.py",
                function_name="search",
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "skill_runtime_prerequisites_unsatisfied",
            result["error_code"],
        )
        self.assertEqual("function", result["invocation_mode"])
        self.assertFalse(result["managed_fallback"])
        self.isolated.assert_not_awaited()

    async def test_rejects_dotted_private_imported_and_symlink_functions_before_dispatch(self) -> None:
        script = self.write_script(
            "from time import sleep\n"
            "def _private():\n    return 'hidden'\n"
            "def public():\n    return 'ok'\n"
        )
        for forbidden in ("os.system", "_private", "sleep"):
            with self.subTest(function_name=forbidden):
                result = await self.call(
                    script_path="skills/database-helper/scripts/helper.py",
                    function_name=forbidden,
                )
                self.assertEqual("error", result["status"])
                self.assertIn("public", [item["name"] for item in result["available_functions"]])
        self.isolated.assert_not_awaited()

        linked = script.with_name("linked.py")
        try:
            linked.symlink_to(script.name)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        result = await self.call(
            script_path="skills/database-helper/scripts/linked.py",
            function_name="public",
        )
        self.assertEqual("error", result["status"])
        self.assertIn("Symlinks are not allowed", result["error"])

    async def test_instance_method_ast_gate_rejects_non_plain_and_mixed_targets(self) -> None:
        self.write_script(
            "from collections import Counter\n"
            "def passthrough(fn):\n"
            "    return fn\n"
            "class QueryClient:\n"
            "    def public(self, value=None):\n"
            "        return value\n"
            "    def _private(self):\n"
            "        return None\n"
            "    @staticmethod\n"
            "    def static(value=None):\n"
            "        return value\n"
            "    @classmethod\n"
            "    def class_call(cls):\n"
            "        return cls.__name__\n"
            "    @property\n"
            "    def property_call(self):\n"
            "        return 1\n"
            "    @passthrough\n"
            "    def decorated(self):\n"
            "        return 1\n"
        )

        forbidden = [
            {"class_name": "os.system", "method_name": "public"},
            {"class_name": "_Private", "method_name": "public"},
            {"class_name": "Counter", "method_name": "most_common"},
            {"class_name": "QueryClient", "method_name": "_private"},
            {"class_name": "QueryClient", "method_name": "static"},
            {"class_name": "QueryClient", "method_name": "class_call"},
            {"class_name": "QueryClient", "method_name": "property_call"},
            {"class_name": "QueryClient", "method_name": "decorated"},
        ]
        for invocation in forbidden:
            with self.subTest(invocation=invocation):
                result = await self.call(
                    script_path="skills/database-helper/scripts/helper.py",
                    **invocation,
                )
                self.assertEqual("error", result["status"])
                available = {
                    (item["name"], method["name"])
                    for item in result.get("available_classes", [])
                    for method in item.get("methods", [])
                }
                self.assertIn(("QueryClient", "public"), available)

        mixed = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            function_name="passthrough",
            class_name="QueryClient",
            method_name="public",
        )
        self.assertEqual("error", mixed["status"])
        self.assertEqual("mixed_invocation_modes", mixed["error_code"])
        self.isolated.assert_not_awaited()

    async def test_workspace_python_fails_closed_without_executor_or_harness_process(self) -> None:
        workspace = self.sandboxes / self.user_id / self.session_id / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        script = workspace / "job.py"
        script.write_text("print('must not run')\n", encoding="utf-8")

        with patch.object(skill_python, "validate_path", return_value=script):
            result = await self.call(script_path="workspace/job.py")

        self.assertEqual("error", result["status"])
        self.assertEqual("workspace_skill_execution_forbidden", result["error_code"])
        self.assertTrue(result["isolated_execution"])
        self.assertFalse(result["managed_fallback"])
        self.isolated.assert_not_awaited()

    async def test_executor_failure_is_structured_and_never_falls_back(self) -> None:
        self.write_script("def public():\n    return 'ok'\n")
        self.isolated.side_effect = IsolatedSkillExecutorError(
            "executor_unavailable", "isolated executor unavailable"
        )

        result = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            function_name="public",
        )

        self.assertEqual("error", result["status"])
        self.assertEqual("executor_unavailable", result["error_code"])
        self.assertEqual("disabled", result["network"])
        self.assertFalse(result["managed_fallback"])

    async def test_json_bounds_and_function_cli_mixing_fail_before_dispatch(self) -> None:
        self.write_script(
            "def echo(value):\n"
            "    return value\n"
            "class EchoClient:\n"
            "    def echo(self, value):\n"
            "        return value\n"
        )
        nested: object = "leaf"
        for _ in range(skill_python.MAX_FUNCTION_JSON_DEPTH + 2):
            nested = [nested]
        result = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            function_name="echo",
            function_args=[nested],
        )
        self.assertEqual("error", result["status"])
        self.assertIn("nesting depth", result["error"])

        result = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            function_name="echo",
            args=["mixed"],
        )
        self.assertEqual("error", result["status"])
        self.assertIn("Do not combine CLI args", result["error"])

        result = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            args=["mixed"],
            class_name="EchoClient",
            method_name="echo",
            method_args=["value"],
        )
        self.assertEqual("error", result["status"])
        self.assertIn("Do not combine CLI args", result["error"])

        result = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            class_name="EchoClient",
            method_name="echo",
            constructor_args=[nested],
            method_args=["value"],
        )
        self.assertEqual("error", result["status"])
        self.assertIn("nesting depth", result["error"])
        self.isolated.assert_not_awaited()

    async def test_all_isolated_artifact_receipts_are_preserved(self) -> None:
        self.write_script("def produce():\n    return 'ok'\n")
        self.isolated.return_value["artifacts"] = [
            {"kind": "file", "path": f"output_result/{index:03d}.txt"}
            for index in range(75)
        ]

        result = await self.call(
            script_path="skills/database-helper/scripts/helper.py",
            function_name="produce",
        )

        self.assertEqual(75, len(result["artifacts"]))
        self.assertEqual(75, result["artifact_total"])

    async def test_legacy_managed_code_adapter_also_uses_session_code_sidecar(self) -> None:
        session_code = AsyncMock(return_value={
            "protocol_version": 1,
            "kind": "session_code_result",
            "status": "success",
            "stdout": "42\n",
            "stderr": "",
            "network": "disabled",
            "artifacts": [],
        })
        with patch.object(skill_python, "execute_isolated_session_code", session_code):
            result = json.loads(await skill_python.run_managed_python_code(
                "print(6 * 7)",
                user_id=self.user_id,
                session_id=self.session_id,
            ))

        self.assertEqual("success", result["status"])
        self.assertEqual("42\n", result["stdout"])
        self.assertTrue(result["isolated_execution"])
        self.assertFalse(result["managed_fallback"])
        self.assertEqual("print(6 * 7)", session_code.await_args.kwargs["code"])


if __name__ == "__main__":
    unittest.main()
