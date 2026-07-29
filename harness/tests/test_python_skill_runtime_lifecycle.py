from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from runtime import python_env
from skills.dependencies import aggregate_dependency_reports
from tools import skill_python


class PythonRuntimeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.skills = self.root / "skills"
        self.runtimes = self.root / "runtimes"
        self.user_id = "runtime-user"
        self.session_id = "runtime-session"
        self.session_root = self.skills / self.user_id / self.session_id
        self.session_root.mkdir(parents=True)
        self.patches = [
            patch.object(python_env, "USER_SKILLS_BASE", self.skills),
            patch.object(python_env, "RUNTIME_ROOT", self.runtimes),
            patch.object(skill_python, "USER_SKILLS_BASE", self.skills),
            patch("tools.skill_script.skill_scanner.USER_SKILLS_BASE", self.skills),
        ]
        for active_patch in self.patches:
            active_patch.start()
        python_env._INITIALIZATION_FLIGHTS.clear()

    def tearDown(self) -> None:
        python_env._INITIALIZATION_FLIGHTS.clear()
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.tempdir.cleanup()

    def make_skill(self, name: str, requirement: str) -> Path:
        root = self.session_root / name
        (root / "scripts").mkdir(parents=True)
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test\n---\n",
            encoding="utf-8",
        )
        (root / "requirements.txt").write_text(requirement + "\n", encoding="utf-8")
        (root / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        return root


class DependencyScopeTests(PythonRuntimeFixture):
    def test_isolated_runtime_probe_unavailable_fails_closed(self) -> None:
        from tools.isolated_skill_executor import IsolatedSkillExecutorError

        with patch(
            "tools.isolated_skill_executor.probe_isolated_runtime_capabilities",
            side_effect=IsolatedSkillExecutorError(
                "executor_unavailable", "socket absent"
            ),
        ):
            result = python_env.preflight_isolated_skill_runtime(
                requirements=["numpy>=2"],
            )

        self.assertFalse(result["valid"])
        self.assertEqual("executor_unavailable", result["error_code"])
        self.assertEqual(
            "isolated_executor_preflight_unavailable",
            result["blockers"][0]["code"],
        )
        self.assertEqual("executor_unavailable", result["packages"]["status"])

    def test_finance_skill_preflight_uses_sidecar_not_harness_environment(self) -> None:
        capability_response = {
            "valid": False,
            "runtime_identity": {
                "execution_runtime": "isolated_skill_executor",
                "python_implementation": "cpython",
                "python_version": "3.12.1",
                "platform": "linux",
                "network": "disabled",
                "dependency_install": "disabled",
            },
            "requirements": [{
                "requirement": "pandas>=2",
                "status": "satisfied",
                "satisfied": True,
                "installed_version": "2.3.0",
            }],
            "commands": [{"name": "python", "available": True}],
            "environment_variables": [{
                "name": "DATA_VENDOR_KEY",
                "available": False,
            }],
            "platform_groups": [{
                "allowed": ["linux"],
                "current": "linux",
                "satisfied": True,
            }],
        }
        with (
            patch.dict(
                os.environ,
                {"DATA_VENDOR_KEY": "harness-only-secret"},
                clear=True,
            ),
            patch(
                "tools.isolated_skill_executor.probe_isolated_runtime_capabilities",
                return_value=capability_response,
            ) as probe,
        ):
            result = python_env.preflight_isolated_skill_runtime(
                requirements=["pandas>=2"],
                commands=["python"],
                environment_variables=["DATA_VENDOR_KEY"],
                platform_groups=[{"allowed": ["linux"], "source_file": "SKILL.md"}],
            )

        self.assertFalse(result["valid"])
        blockers = {item["code"]: item["items"] for item in result["blockers"]}
        self.assertEqual(
            ["DATA_VENDOR_KEY"],
            blockers["missing_required_environment_variables"],
        )
        self.assertEqual("satisfied", result["packages"]["status"])
        self.assertNotIn("harness-only-secret", repr(result))
        probe.assert_called_once_with(
            requirements=["pandas>=2"],
            commands=["python"],
            environment_variables=["DATA_VENDOR_KEY"],
            platform_groups=[["linux"]],
            socket_path="/run/chat-ds-executor/executor.sock",
        )

    def test_target_skill_dependencies_do_not_include_session_siblings(self) -> None:
        target = self.make_skill("target-skill", "target-package==1")
        self.make_skill("unrelated-skill", "unrelated-package==2")

        target_manifest = aggregate_dependency_reports(
            python_env._scan_session_reports(
                self.user_id,
                self.session_id,
                target_skill_dir=target,
            )
        )
        session_manifest = aggregate_dependency_reports(
            python_env._scan_session_reports(self.user_id, self.session_id)
        )

        self.assertEqual(target_manifest["python_packages"], ["target-package==1"])
        self.assertCountEqual(
            session_manifest["python_packages"],
            ["target-package==1", "unrelated-package==2"],
        )

    def test_skill_entrypoint_selects_owner_but_workspace_code_keeps_session_scope(self) -> None:
        target = self.make_skill("target-skill", "target-package==1")
        workspace_script = self.root / "workspace" / "job.py"
        workspace_script.parent.mkdir()
        workspace_script.write_text("print('workspace')\n", encoding="utf-8")

        target_scope = skill_python._owning_session_skill_dir(
            target / "scripts" / "run.py", self.user_id, self.session_id
        )
        workspace_scope = skill_python._owning_session_skill_dir(
            workspace_script, self.user_id, self.session_id
        )
        self.assertEqual(target_scope, target.resolve())
        self.assertIsNone(workspace_scope)

    def test_target_runtime_pythonpath_excludes_sibling_skills(self) -> None:
        target = self.make_skill("target-skill", "")
        sibling = self.make_skill("sibling-skill", "")
        status = {
            "status": "ready",
            "manifest": {
                "dependency_scope": "target_skill",
                "target_skill_dir": str(target),
            },
        }
        env = python_env.runtime_env_for_subprocess(
            status,
            {"PATH": "/usr/bin"},
            user_id=self.user_id,
            session_id=self.session_id,
        )
        paths = env.get("PYTHONPATH", "").split(os.pathsep)
        self.assertIn(str(target.resolve()), paths)
        self.assertNotIn(str(sibling.resolve()), paths)

    def test_pip_environment_does_not_inherit_harness_secrets_or_indexes(self) -> None:
        with patch.dict(os.environ, {
            "PATH": "/usr/bin",
            "INTERNAL_API_TOKEN": "must-not-leak",
            "PIP_EXTRA_INDEX_URL": "https://credential.invalid/simple",
        }, clear=True):
            env = python_env._pip_env()
        self.assertEqual("/usr/bin", env["PATH"])
        self.assertNotIn("INTERNAL_API_TOKEN", env)
        self.assertNotIn("PIP_EXTRA_INDEX_URL", env)


class RuntimeInitializationTests(PythonRuntimeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_direct_url_requirement_is_blocked_before_installer(self) -> None:
        target = self.make_skill(
            "unsafe-dependency",
            "demo @ https://example.invalid/demo.whl",
        )
        with patch.object(
            python_env,
            "_run_command",
            AsyncMock(side_effect=AssertionError("installer must not run")),
        ):
            status = await python_env.ensure_session_runtime(
                self.user_id,
                self.session_id,
                target_skill_dir=target,
            )
        self.assertEqual("unsupported", status["status"])
        self.assertIn(
            "unsafe_python_requirement",
            " ".join(status["manifest"]["unsupported"]),
        )

    async def test_equivalent_concurrent_initialization_is_singleflight(self) -> None:
        target = self.make_skill("target-skill", "singleflight-package==1")
        install_started = asyncio.Event()
        release_install = asyncio.Event()
        package_installs = 0

        async def fake_run_command(cmd, *, timeout, log_path=None):
            nonlocal package_installs
            if len(cmd) >= 4 and cmd[1:3] == ["-m", "venv"]:
                venv = Path(cmd[-1])
                (venv / "bin").mkdir(parents=True, exist_ok=True)
                (venv / "bin" / "python").write_text("", encoding="utf-8")
                (venv / "bin" / "pip").write_text("", encoding="utf-8")
            if "singleflight-package==1" in cmd:
                package_installs += 1
                install_started.set()
                await release_install.wait()
            if cmd[-1:] == ["freeze"]:
                return {"stdout": "singleflight-package==1\n", "stderr": ""}
            return {"stdout": "", "stderr": ""}

        with patch.object(python_env, "_run_command", side_effect=fake_run_command):
            first = asyncio.create_task(
                python_env.ensure_session_runtime(
                    self.user_id,
                    self.session_id,
                    target_skill_dir=target,
                )
            )
            await asyncio.wait_for(install_started.wait(), timeout=1)
            second = asyncio.create_task(
                python_env.ensure_session_runtime(
                    self.user_id,
                    self.session_id,
                    target_skill_dir=target,
                )
            )
            joined_same_flight = False
            for _ in range(20):
                if any(flight.callers == 2 for flight in python_env._INITIALIZATION_FLIGHTS.values()):
                    joined_same_flight = True
                    break
                await asyncio.sleep(0)
            self.assertTrue(joined_same_flight)
            release_install.set()
            first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(first_result["status"], "ready")
        self.assertEqual(second_result["status"], "ready")
        self.assertEqual(first_result["env_hash"], second_result["env_hash"])
        self.assertEqual(package_installs, 1)
        self.assertFalse(python_env._INITIALIZATION_FLIGHTS)

    async def test_install_failure_is_negatively_cached_for_short_retry_window(self) -> None:
        target = self.make_skill("target-skill", "broken-package==1")
        commands = 0

        async def failing_command(cmd, *, timeout, log_path=None):
            nonlocal commands
            commands += 1
            raise RuntimeError("index unavailable")

        with (
            patch.object(python_env, "INSTALL_FAILURE_CACHE_SECONDS", 30),
            patch.object(python_env, "_run_command", side_effect=failing_command),
        ):
            first = await python_env.ensure_session_runtime(
                self.user_id,
                self.session_id,
                target_skill_dir=target,
            )
            second = await python_env.ensure_session_runtime(
                self.user_id,
                self.session_id,
                target_skill_dir=target,
            )

        self.assertEqual(first["status"], "install_failed")
        self.assertEqual(second["status"], "install_failed")
        self.assertTrue(second["negative_cache"])
        self.assertGreater(second["retry_after_seconds"], 0)
        self.assertEqual(commands, 1)


class RuntimeCancellationTests(PythonRuntimeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_direct_skill_runner_fails_closed_before_runtime_initialization(self) -> None:
        script = self.root / "job.py"
        script.write_text("print('never starts')\n", encoding="utf-8")
        initialization = AsyncMock(side_effect=AssertionError("must not initialize"))
        create_process = AsyncMock()
        with (
            patch.object(skill_python, "ensure_session_runtime", initialization),
            patch.object(skill_python.asyncio, "create_subprocess_exec", create_process),
        ):
            result = json.loads(await skill_python._run_python_script(
                script,
                args=[],
                timeout=1,
                cwd=self.root,
                user_id=self.user_id,
                session_id=self.session_id,
            ))

        initialization.assert_not_awaited()
        create_process.assert_not_awaited()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "direct_python_execution_disabled")
        self.assertTrue(result["isolated_execution"])

    async def test_private_direct_initializer_is_not_a_bypass(self) -> None:
        script = self.root / "job.py"
        script.write_text("print('never starts')\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "isolated executor"):
            await skill_python._initialize_and_run_python_script(
                script,
                args=[],
                cwd=self.root,
                user_id=self.user_id,
                session_id=self.session_id,
                managed_fallback=False,
                lifecycle={},
            )

    async def test_cancelling_installer_kills_and_reaps_subprocess(self) -> None:
        class BlockingProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.started = asyncio.Event()
                self.killed = False
                self.communicate_calls = 0

            async def communicate(self):
                self.communicate_calls += 1
                self.started.set()
                if not self.killed:
                    await asyncio.Event().wait()
                return b"", b""

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            async def wait(self):
                return self.returncode

        process = BlockingProcess()
        with patch.object(
            python_env.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            task = asyncio.create_task(
                python_env._run_command(["python", "-m", "venv", "x"], timeout=60)
            )
            await asyncio.wait_for(process.started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.communicate_calls, 2)


class AtomicStatusTests(PythonRuntimeFixture):
    def test_session_runtime_cleanup_is_nofollow_and_session_scoped(self) -> None:
        session_runtime = (
            self.runtimes / self.user_id / self.session_id
        )
        session_runtime.mkdir(parents=True)
        (session_runtime / "state.json").write_text(
            "{}",
            encoding="utf-8",
        )
        sibling = self.runtimes / self.user_id / "other-session"
        sibling.mkdir()

        self.assertTrue(
            python_env.clean_session_runtime(
                self.user_id,
                self.session_id,
            )
        )
        self.assertFalse(session_runtime.exists())
        self.assertTrue(sibling.is_dir())

    def test_session_runtime_cleanup_rejects_user_symlink(self) -> None:
        self.runtimes.mkdir()
        outside = self.root / "outside-runtime"
        (outside / self.session_id).mkdir(parents=True)
        (self.runtimes / self.user_id).symlink_to(
            outside,
            target_is_directory=True,
        )
        with self.assertRaises(OSError):
            python_env.clean_session_runtime(
                self.user_id,
                self.session_id,
            )
        self.assertTrue((outside / self.session_id).is_dir())

    def test_concurrent_status_writes_use_distinct_atomic_temp_files(self) -> None:
        status_path = self.root / "shared" / "status.json"
        barrier = threading.Barrier(2)
        sources: list[str] = []
        original_replace = os.replace

        def synchronized_replace(source, destination):
            sources.append(str(source))
            barrier.wait(timeout=2)
            original_replace(source, destination)

        def write(value: int) -> None:
            python_env._write_status_file(status_path, {"value": value})

        with patch.object(python_env.os, "replace", side_effect=synchronized_replace):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(write, value) for value in (1, 2)]
                for future in futures:
                    future.result(timeout=3)

        self.assertEqual(len(sources), 2)
        self.assertEqual(len(set(sources)), 2)
        self.assertIn(json.loads(status_path.read_text(encoding="utf-8"))["value"], {1, 2})
        self.assertFalse([
            path for path in status_path.parent.iterdir() if path.name.endswith(".tmp")
        ])


if __name__ == "__main__":
    unittest.main()
