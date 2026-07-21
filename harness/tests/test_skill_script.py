from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tools import skill_script
from tools.registry import get_metadata, get_schemas


class SkillScriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.skills = self.root / "skills"
        self.sandboxes = self.root / "sandboxes"
        self.user_id = "script-user"
        self.session_id = "script-session"
        self.skill = self.skills / self.user_id / self.session_id / "portable-skill"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(
            "---\nname: portable-skill\ndescription: portable scripts\n---\n",
            encoding="utf-8",
        )

        def fake_sandbox_dir(user_id: str, session_id: str, sub: str = "files") -> Path:
            path = (self.sandboxes / user_id / session_id / sub).resolve()
            path.mkdir(parents=True, exist_ok=True)
            return path

        self.patches = [
            patch.object(skill_script, "USER_SKILLS_BASE", self.skills),
            patch.object(skill_script.skill_scanner, "USER_SKILLS_BASE", self.skills),
            patch.object(skill_script, "sandbox_dir", side_effect=fake_sandbox_dir),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.tempdir.cleanup()

    def write_script(self, name: str, content: str) -> Path:
        path = self.skill / "scripts" / name
        path.write_text(content, encoding="utf-8")
        return path

    async def call(self, **kwargs):
        raw = await skill_script.run_skill_script(
            user_id=self.user_id,
            session_id=self.session_id,
            **kwargs,
        )
        return json.loads(raw)

    def test_schema_and_registry_expose_only_declarative_controls(self) -> None:
        properties = skill_script.RUN_SKILL_SCRIPT_SCHEMA["parameters"]["properties"]
        self.assertEqual(
            {".py", ".sh", ".bash", ".js", ".mjs"},
            set(skill_script.SUPPORTED_EXTENSIONS),
        )
        self.assertNotIn("interpreter", properties)
        self.assertNotIn("command", properties)
        self.assertEqual(["workspace", "script"], properties["cwd"]["enum"])
        schemas = get_schemas(["run_skill_script"])
        self.assertEqual("run_skill_script", schemas[0]["function"]["name"])
        metadata = get_metadata("run_skill_script") or {}
        self.assertTrue(metadata.get("path_scoped"))
        self.assertTrue(metadata.get("mutates_workspace"))
        self.assertFalse(metadata.get("allow_in_parallel_child"))

    async def test_python_is_delegated_to_isolated_executor_after_path_validation(self) -> None:
        self.write_script("task.py", "print('isolated')\n")
        isolated = AsyncMock(return_value={
            "status": "success",
            "stdout": "isolated\n",
            "artifacts": [],
            "interpreter": "python",
            "network": "disabled",
        })
        with patch.object(skill_script, "execute_isolated_skill_script", isolated):
            result = await self.call(
                script_path="skills/portable-skill/scripts/task.py",
                args=["literal"],
                timeout=25,
                cwd="script",
            )

        self.assertEqual("success", result["status"])
        self.assertEqual("isolated_skill_executor", result["execution_runtime"])
        self.assertEqual("python", result["interpreter"])
        isolated.assert_awaited_once_with(
            skill_root=self.skill.resolve(),
            workspace=(self.sandboxes / self.user_id / self.session_id / "workspace").resolve(),
            entrypoint="scripts/task.py",
            args=["literal"],
            timeout=25,
            cwd="script",
        )

    async def test_promoted_user_skill_runs_only_when_runtime_enabled(self) -> None:
        promoted = self.skills / self.user_id / "promoted-skill"
        (promoted / "scripts").mkdir(parents=True)
        (promoted / "SKILL.md").write_text(
            "---\nname: promoted-skill\ndescription: promoted executable\n---\n",
            encoding="utf-8",
        )
        (promoted / "scripts" / "task.py").write_text(
            "print('promoted')\n",
            encoding="utf-8",
        )
        isolated = AsyncMock(return_value={
            "status": "success",
            "stdout": "promoted\n",
            "artifacts": [],
            "interpreter": "python",
            "network": "disabled",
        })
        with patch.object(skill_script, "execute_isolated_skill_script", isolated):
            denied = await self.call(
                script_path="skills/promoted-skill/scripts/task.py",
            )
            allowed = await self.call(
                script_path="skills/promoted-skill/scripts/task.py",
                enabled_user_skills=["promoted-skill"],
            )

        self.assertEqual("error", denied["status"])
        self.assertEqual("unknown_session_skill", denied["error_code"])
        self.assertEqual("success", allowed["status"])
        self.assertEqual(promoted.resolve(), isolated.await_args.kwargs["skill_root"])

    async def test_frontmatter_name_directory_mismatch_is_not_runnable(self) -> None:
        mismatched = (
            self.skills / self.user_id / self.session_id / "physical-directory"
        )
        (mismatched / "scripts").mkdir(parents=True)
        (mismatched / "SKILL.md").write_text(
            "---\nname: canonical-script-skill\ndescription: mismatch fixture\n---\n",
            encoding="utf-8",
        )
        (mismatched / "scripts" / "task.py").write_text(
            "print('managed')\n", encoding="utf-8"
        )
        isolated = AsyncMock(return_value={
            "status": "success",
            "artifacts": [],
            "network": "disabled",
        })
        with patch.object(skill_script, "execute_isolated_skill_script", isolated):
            result = await self.call(
                script_path="skills/canonical-script-skill/scripts/task.py",
            )
            alias_result = await self.call(
                script_path="skills/physical-directory/scripts/task.py",
            )

        self.assertEqual("error", result["status"])
        self.assertEqual("unknown_session_skill", result["error_code"])
        self.assertEqual("error", alias_result["status"])
        self.assertEqual("unknown_session_skill", alias_result["error_code"])
        isolated.assert_not_awaited()

    async def test_bash_receives_literal_argv_safe_env_and_returns_artifact_receipt(self) -> None:
        self.write_script(
            "task.sh",
            "printf '%s' \"$1\"\n"
            "printf '%s' \"${SECRET_TOKEN-unset}\" >&2\n"
            "mkdir -p \"$CHATDS_OUTPUT_DIR\"\n"
            "printf 'artifact' > \"$CHATDS_OUTPUT_DIR/result.txt\"\n",
        )
        literal = "$(touch should-not-run); literal | value"
        isolated = AsyncMock(return_value={
            "status": "success",
            "stdout": literal,
            "stderr": "unset",
            "interpreter": "bash",
            "network": "disabled",
            "artifacts": [{
                "kind": "file",
                "path": "output_result/result.txt",
                "change": "created",
                "size_bytes": 8,
                "sha256": "a" * 64,
            }],
        })
        with (
            patch.dict(os.environ, {"SECRET_TOKEN": "must-not-leak"}),
            patch.object(skill_script, "execute_isolated_skill_script", isolated),
        ):
            result = await self.call(
                script_path="portable-skill/scripts/task.sh",
                args=[literal],
            )

        self.assertEqual("success", result["status"])
        self.assertEqual(literal, result["stdout"])
        self.assertEqual("unset", result["stderr"])
        self.assertEqual("bash", result["interpreter"])
        self.assertEqual("isolated_skill_executor", result["execution_runtime"])
        self.assertFalse(result["fallback_attempted"])
        self.assertFalse(
            (self.sandboxes / self.user_id / self.session_id / "workspace" / "should-not-run").exists()
        )
        receipts = {item["path"]: item for item in result["artifacts"]}
        self.assertEqual("created", receipts["output_result/result.txt"]["change"])
        self.assertEqual(8, receipts["output_result/result.txt"]["size_bytes"])
        self.assertEqual([literal], isolated.await_args.kwargs["args"])

    async def test_executor_unavailable_is_structured_and_never_falls_back(self) -> None:
        self.write_script("task.mjs", "console.log('no fallback');\n")
        failure = skill_script.IsolatedSkillExecutorError(
            "executor_unavailable", "isolated executor unavailable"
        )
        with patch.object(
            skill_script,
            "execute_isolated_skill_script",
            new=AsyncMock(side_effect=failure),
        ) as execute:
            result = await self.call(
                script_path="skills/portable-skill/scripts/task.mjs",
            )

        self.assertEqual("error", result["status"])
        self.assertEqual("executor_unavailable", result["error_code"])
        self.assertEqual("isolated_skill_executor", result["execution_runtime"])
        self.assertFalse(result["fallback_attempted"])
        execute.assert_awaited_once()

    async def test_declared_dependency_preflight_blocks_before_executor_dispatch(self) -> None:
        self.write_script("task.py", "raise SystemExit('must not run')\n")
        isolated = AsyncMock()
        with (
            patch.object(
                skill_script,
                "preflight_declared_skill_dependencies",
                return_value={
                    "valid": False,
                    "checked": True,
                    "blockers": [{
                        "code": "unsatisfied_python_dependencies",
                        "items": [{
                            "requirement": "forecasting-lib>=4",
                            "status": "missing",
                        }],
                    }],
                    "packages": {
                        "requirements": ["forecasting-lib>=4"],
                        "status": "unsatisfied",
                    },
                },
            ),
            patch.object(skill_script, "execute_isolated_skill_script", isolated),
        ):
            result = await self.call(
                script_path="skills/portable-skill/scripts/task.py",
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "skill_runtime_prerequisites_unsatisfied",
            result["error_code"],
        )
        self.assertFalse(result["fallback_attempted"])
        isolated.assert_not_awaited()

    async def test_node_script_uses_literal_argv_and_emits_artifact_receipt(self) -> None:
        self.write_script(
            "task.js",
            "const fs = require('fs');\n"
            "const path = require('path');\n"
            "fs.mkdirSync(process.env.CHATDS_OUTPUT_DIR, {recursive: true});\n"
            "fs.writeFileSync(path.join(process.env.CHATDS_OUTPUT_DIR, 'node.txt'), process.argv[2]);\n"
            "process.stdout.write(process.argv[2]);\n",
        )
        literal = "$(touch node-should-not-run); literal"
        isolated = AsyncMock(return_value={
            "status": "success",
            "stdout": literal,
            "stderr": "",
            "interpreter": "node",
            "network": "disabled",
            "artifacts": [{
                "kind": "file",
                "path": "output_result/node.txt",
                "change": "created",
                "size_bytes": len(literal),
                "sha256": "b" * 64,
            }],
        })
        with patch.object(skill_script, "execute_isolated_skill_script", isolated):
            result = await self.call(
                script_path="portable-skill/scripts/task.js",
                args=[literal],
            )

        self.assertEqual("success", result["status"])
        self.assertEqual("node", result["interpreter"])
        self.assertEqual(literal, result["stdout"])
        self.assertIn(
            "output_result/node.txt",
            {item["path"] for item in result["artifacts"]},
        )

    async def test_linked_workspace_output_directory_fails_before_execution(self) -> None:
        self.write_script("task.sh", "printf unsafe > \"$CHATDS_OUTPUT_DIR/file.txt\"\n")
        workspace = self.sandboxes / self.user_id / self.session_id / "workspace"
        workspace.mkdir(parents=True)
        outside = self.root / "outside-output"
        outside.mkdir()
        try:
            (workspace / "output_result").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        result = await self.call(
            script_path="portable-skill/scripts/task.sh",
        )

        self.assertEqual("error", result["status"])
        self.assertEqual("unsafe_snapshot_file", result["error_code"])
        self.assertFalse(result["fallback_attempted"])
        self.assertEqual([], list(outside.iterdir()))

    async def test_traversal_symlink_workspace_and_unowned_paths_fail_closed(self) -> None:
        self.write_script("safe.sh", "printf safe\n")
        outside = self.root / "outside.sh"
        outside.write_text("printf outside\n", encoding="utf-8")
        linked = self.skill / "scripts" / "linked.sh"
        try:
            linked.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        unowned = self.skills / self.user_id / self.session_id / "loose" / "task.sh"
        unowned.parent.mkdir()
        unowned.write_text("printf loose\n", encoding="utf-8")

        cases = (
            ("skills/../outside.sh", "invalid_script_path"),
            ("workspace/task.sh", "unknown_session_skill"),
            ("skills/portable-skill/scripts/linked.sh", "symlink_resource_path"),
            ("skills/loose/task.sh", "unknown_session_skill"),
            (str(outside), "invalid_script_path"),
        )
        for path, code in cases:
            with self.subTest(path=path):
                result = await self.call(script_path=path)
                self.assertEqual("error", result["status"])
                self.assertEqual(code, result["error_code"])

    async def test_argument_timeout_cwd_and_extension_limits_fail_before_spawn(self) -> None:
        self.write_script("safe.sh", "printf safe\n")
        self.write_script("unsafe.txt", "not executable\n")
        cases = (
            ({"script_path": "portable-skill/scripts/safe.sh", "args": ["x"] * 65}, "argument_limit_exceeded"),
            ({"script_path": "portable-skill/scripts/safe.sh", "args": ["\x00"]}, "invalid_args"),
            ({"script_path": "portable-skill/scripts/safe.sh", "timeout": 301}, "invalid_timeout"),
            ({"script_path": "portable-skill/scripts/safe.sh", "cwd": "skill"}, "invalid_cwd"),
            ({"script_path": "portable-skill/scripts/unsafe.txt"}, "unsupported_script_type"),
        )
        with patch.object(
            skill_script,
            "execute_isolated_skill_script",
            new=AsyncMock(),
        ) as execute:
            for invocation, code in cases:
                with self.subTest(code=code):
                    result = await self.call(**invocation)
                    self.assertEqual("error", result["status"])
                    self.assertEqual(code, result["error_code"])
        execute.assert_not_awaited()

    async def test_timeout_kills_shell_process_group_and_returns_receipts(self) -> None:
        self.write_script(
            "slow.bash",
            "mkdir -p \"$CHATDS_OUTPUT_DIR\"\n"
            "printf started > \"$CHATDS_OUTPUT_DIR/started.txt\"\n"
            "sleep 5\n",
        )

        isolated = AsyncMock(return_value={
            "status": "timeout",
            "error_code": "script_timeout",
            "error": "Skill script timed out after 1s.",
            "returncode": -9,
            "interpreter": "bash",
            "network": "disabled",
            "artifacts": [{
                "kind": "file",
                "path": "output_result/started.txt",
                "change": "created",
                "size_bytes": 7,
                "sha256": "c" * 64,
            }],
        })
        with patch.object(skill_script, "execute_isolated_skill_script", isolated):
            result = await self.call(
                script_path="portable-skill/scripts/slow.bash",
                timeout=1,
            )

        self.assertEqual("timeout", result["status"])
        self.assertEqual("script_timeout", result["error_code"])
        self.assertFalse(result["fallback_attempted"])
        self.assertIn(
            "output_result/started.txt",
            {item["path"] for item in result["artifacts"]},
        )


if __name__ == "__main__":
    unittest.main()
