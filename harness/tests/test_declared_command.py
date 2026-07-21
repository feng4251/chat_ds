from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from skills.command_grants import (
    all_compiled_command_grants,
    command_grant_from_selector,
    compile_environment_command_grants,
    grant_tuple,
    grants_for_declaration,
    parse_allowed_tool_selectors,
    load_current_skill_command_grants,
    scope_command_grant,
    selected_plan_command_grants,
)
from skills.loader import load_skill_content
from agent_loop import _bounded_skill_execution_exposure
from tools.context import ToolContext
from tools.declared_command import RUN_DECLARED_COMMAND_SCHEMA, run_declared_command
from tools.delegation import _exact_declared_skill_command_grants
from tools.registry import ToolRegistry


class CommandGrantCompilerTests(unittest.TestCase):
    def test_allowed_tools_scalar_preserves_spaces_inside_selector(self) -> None:
        self.assertEqual(
            ["Read", "Shell(git status:*)", "execute_code"],
            parse_allowed_tool_selectors(
                "Read, Shell(git status:*) execute_code"
            ),
        )
        self.assertEqual(
            ["Shell(git status:* Read"],
            parse_allowed_tool_selectors("Shell(git status:* Read"),
        )

    def test_capability_command_grant_flows_root_to_exact_child_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            session = base / "u" / "s"
            main = session / "main-report"
            cap = session / "cap-cli"
            other = session / "other-cli"
            (main / "orchestration").mkdir(parents=True)
            (cap / "orchestration").mkdir(parents=True)
            other.mkdir(parents=True)
            (main / "SKILL.md").write_text(
                "---\nname: main-report\ndescription: fixture\n---\n",
                encoding="utf-8",
            )
            (main / "orchestration" / "workflow.yaml").write_text(
                """
orchestrator_id: main
workers:
  writer:
    role: Write report.
    skills: [cap-cli]
routing_rules:
  run:
    patterns: [run]
    workers: [writer]
""".strip(),
                encoding="utf-8",
            )
            for root, name in ((cap, "cap-cli"), (other, "other-cli")):
                (root / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: fixture\n"
                    "allowed-tools:\n  - \"Bash(git status:*)\"\n---\n",
                    encoding="utf-8",
                )
            (cap / "orchestration" / "workflow.yaml").write_text(
                """
orchestrator_id: cap
workers:
  private-node:
    role: Private node.
    tools: ["Bash(git status:*)"]
""".strip(),
                encoding="utf-8",
            )
            loaded = {}
            for name, root in (
                ("main-report", main), ("cap-cli", cap), ("other-cli", other)
            ):
                package = load_skill_content(
                    root / "SKILL.md", skill_dir=str(root), session_id="s"
                )
                package["_chatds_scope"] = "session"
                loaded[name] = package

            exposure = _bounded_skill_execution_exposure(
                "run main-report",
                ["skills_list", "skill_view", "delegate_task", "run_declared_command"],
                set(loaded),
                loaded,
                {},
            )
            cap_root_grants = [
                item for item in exposure.allowed_skill_commands
                if item[0] == "cap-cli"
            ]
            self.assertEqual(1, len(cap_root_grants))
            self.assertFalse(any(item[0] == "other-cli" for item in exposure.allowed_skill_commands))

            with patch("skills.scanner.USER_SKILLS_BASE", base):
                _root, _package, cap_current = __import__(
                    "skills.command_grants", fromlist=["load_current_skill_command_grants"]
                ).load_current_skill_command_grants("cap-cli", "u", "s")
                private = next(
                    grant_tuple("cap-cli", item) for item in cap_current
                    if item.get("scope") == "worker:private-node"
                )
                other_package = next(
                    grant_tuple("other-cli", item)
                    for item in all_compiled_command_grants(loaded["other-cli"])
                    if item.get("scope") == "package"
                )
                context = ToolContext(
                    user_id="u",
                    session_id="s",
                    skill_execution_resource_boundary=True,
                    allowed_skill_commands=tuple(
                        cap_root_grants + [private, other_package]
                    ),
                )
                child = _exact_declared_skill_command_grants(
                    task={"skill_name": "main-report", "worker_id": "writer"},
                    required_capability_skills=["cap-cli"],
                    context=context,
                )
            self.assertEqual(cap_root_grants, child)
            self.assertNotIn(private, child)
            self.assertNotIn(other_package, child)

    def test_only_structured_commands_and_narrowed_selectors_compile(self) -> None:
        grants = compile_environment_command_grants(
            [{"name": "python", "source_files": ["SKILL.md"]}],
            ["Bash(git status:*)", "Bash", "Shell", "Bash(*)", "Bash(*:*)"],
        )
        self.assertEqual({"git"}, {item["executable"] for item in grants})
        git = next(item for item in grants if item["executable"] == "git")
        self.assertEqual(["status"], git["argv_prefix"])
        for rejected in (
            "Bash", "Shell", "Bash(*)", "Bash(*:*)", "Bash(/bin/sh:*)",
            "Bash(python $(id):*)", "prose says run python",
        ):
            with self.subTest(selector=rejected):
                self.assertIsNone(command_grant_from_selector(rejected))

    def test_scope_changes_grant_id_and_prevents_cross_worker_reuse(self) -> None:
        base = command_grant_from_selector("Bash(python:*)")
        worker_a = scope_command_grant(base, "worker:a")
        worker_b = scope_command_grant(base, "worker:b")
        self.assertIsNotNone(worker_a)
        self.assertIsNotNone(worker_b)
        self.assertNotEqual(worker_a["id"], worker_b["id"])
        declaration = {
            "tools": ["Bash(python:*)"],
            "environment_contract": {
                "commands": [{"name": "git", "source_files": ["worker.yaml"]}],
            },
        }
        scoped = grants_for_declaration(declaration, scope="worker:a")
        self.assertEqual({"python"}, {item["executable"] for item in scoped})
        self.assertTrue(all(item["scope"] == "worker:a" for item in scoped))

    def test_model_schema_contains_only_capability_fields(self) -> None:
        parameters = RUN_DECLARED_COMMAND_SCHEMA["parameters"]
        self.assertEqual(
            {"skill_name", "command_id", "argv", "cwd"},
            set(parameters["properties"]),
        )
        self.assertFalse(parameters["additionalProperties"])
        self.assertNotIn("command", parameters["properties"])
        self.assertNotIn("executable", parameters["properties"])
        self.assertNotIn("timeout", parameters["properties"])

    def test_loader_selected_scope_roundtrips_through_current_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "orchestration").mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: portable-finance\ndescription: Compile portable finance command grants.\nprerequisites:\n  commands: [python]\n---\n",
                encoding="utf-8",
            )
            (root / "orchestration" / "workflow.yaml").write_text(
                """
orchestrator_id: portable
workers:
  price-check:
    role: Check prices.
    tools: ["Bash(git status:*)"]
routing_rules:
  run:
    patterns: [run]
    workers: [price-check]
""".strip(),
                encoding="utf-8",
            )
            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))
        execution = loaded["execution_contract"]
        worker = execution["workers"][0]
        plan = {
            "workers": {"price-check": worker},
            "required_workers": ["price-check"],
            "bootstrap_sources": [],
            "aggregation_steps": [],
        }
        selected = selected_plan_command_grants(execution, plan)
        current = all_compiled_command_grants(loaded)
        self.assertEqual(
            {item["id"] for item in selected},
            {item["id"] for item in current},
        )
        self.assertEqual({"git"}, {item["executable"] for item in current})


class DeclaredCommandBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        base = command_grant_from_selector("Bash(git status:*)")
        self.grant = scope_command_grant(base, "worker:finance")
        assert self.grant is not None
        self.allowed = grant_tuple("portable-finance", self.grant)
        self.context = ToolContext(
            user_id="u",
            session_id="s",
            agent_kind="delegate",
            allowed_skill_commands=(self.allowed,),
        )

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()

        async def handler(
            skill_name: str,
            command_id: str,
            argv: list[str],
            cwd: str,
            context: ToolContext | None = None,
        ) -> str:
            return json.dumps({"status": "success", "command_id": command_id})

        registry.register(
            name="run_declared_command",
            toolset="test",
            schema=RUN_DECLARED_COMMAND_SCHEMA,
            handler=handler,
        )
        return registry

    async def test_root_and_delegate_dispatch_require_exact_current_grant(self) -> None:
        args = {
            "skill_name": "portable-finance",
            "command_id": self.grant["id"],
            "argv": ["--short"],
            "cwd": "workspace",
        }
        with patch(
            "skills.command_grants.load_current_skill_command_grants",
            return_value=(Path("/tmp/skill"), {}, [self.grant]),
        ):
            result = json.loads(await self._registry().dispatch(
                "run_declared_command", args, context=self.context
            ))
        self.assertEqual("success", result["status"])

        no_context = json.loads(await self._registry().dispatch(
            "run_declared_command", args, context=None
        ))
        self.assertEqual("declared_command_boundary_violation", no_context["reason"])

        wrong = dict(args, command_id="command-" + "0" * 24)
        denied = json.loads(await self._registry().dispatch(
            "run_declared_command", wrong, context=self.context
        ))
        self.assertEqual("declared_command_boundary_violation", denied["reason"])

        with patch(
            "skills.command_grants.load_current_skill_command_grants",
            return_value=(Path("/tmp/skill"), {}, []),
        ):
            changed = json.loads(await self._registry().dispatch(
                "run_declared_command", args, context=self.context
            ))
        self.assertIn("changed after compilation", changed["error"])

    async def test_tool_preserves_literal_argv_and_returns_artifact_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            execute = AsyncMock(return_value={
                "status": "success",
                "shell": False,
                "network": "disabled",
                "stdout": "ok",
                "stderr": "",
                "command_sha256": "1" * 64,
                "artifacts": [{
                    "kind": "file",
                    "path": "prices.json",
                    "size_bytes": 2,
                    "sha256": "2" * 64,
                }],
            })
            with (
                patch(
                    "tools.declared_command.load_current_skill_command_grants",
                    return_value=(root, {}, [self.grant]),
                ),
                patch(
                    "tools.declared_command.preflight_declared_skill_dependencies",
                    return_value={"valid": True},
                ),
                patch(
                    "tools.declared_command.preflight_isolated_skill_runtime",
                    return_value={"valid": True},
                ),
                patch("tools.declared_command.sandbox_dir", return_value=workspace),
                patch(
                    "tools.declared_command.execute_isolated_declared_command",
                    execute,
                ),
            ):
                result = json.loads(await run_declared_command(
                    "portable-finance",
                    self.grant["id"],
                    ["literal; $(touch never) | x"],
                    "workspace",
                    context=self.context,
                ))
        self.assertEqual("success", result["status"])
        self.assertEqual("prices.json", result["artifacts"][0]["path"])
        self.assertEqual(
            ["status", "literal; $(touch never) | x"],
            execute.await_args.kwargs["argv"],
        )
        self.assertNotIn("command", execute.await_args.kwargs)

    async def test_promoted_user_command_requires_enabled_whitelist_and_exact_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            skill = base / "u" / "promoted-cli"
            workspace = base / "workspace"
            skill.mkdir(parents=True)
            workspace.mkdir()
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: promoted-cli\n"
                "description: promoted command fixture\n"
                "allowed-tools:\n"
                "  - \"Bash(git status:*)\"\n"
                "---\n",
                encoding="utf-8",
            )
            execute = AsyncMock(return_value={
                "status": "success",
                "shell": False,
                "network": "disabled",
                "stdout": "clean",
                "stderr": "",
                "artifacts": [],
            })
            with patch("skills.scanner.USER_SKILLS_BASE", base):
                with self.assertRaises(ValueError):
                    load_current_skill_command_grants(
                        "promoted-cli", "u", "s"
                    )
                _root, _loaded, grants = load_current_skill_command_grants(
                    "promoted-cli",
                    "u",
                    "s",
                    ["promoted-cli"],
                )
                package_grant = next(
                    item for item in grants if item.get("scope") == "package"
                )
                context = ToolContext(
                    user_id="u",
                    session_id="s",
                    enabled_user_skills=("promoted-cli",),
                    allowed_skill_commands=(
                        grant_tuple("promoted-cli", package_grant),
                    ),
                )
                with (
                    patch(
                        "tools.declared_command.preflight_declared_skill_dependencies",
                        return_value={"valid": True},
                    ),
                    patch(
                        "tools.declared_command.preflight_isolated_skill_runtime",
                        return_value={"valid": True},
                    ),
                    patch(
                        "tools.declared_command.sandbox_dir",
                        return_value=workspace,
                    ),
                    patch(
                        "tools.declared_command.execute_isolated_declared_command",
                        execute,
                    ),
                ):
                    result = json.loads(await run_declared_command(
                        "promoted-cli",
                        package_grant["id"],
                        ["--short"],
                        "workspace",
                        context=context,
                    ))

        self.assertEqual("success", result["status"])
        self.assertEqual(skill.resolve(), execute.await_args.kwargs["skill_root"])
        self.assertEqual(["status", "--short"], execute.await_args.kwargs["argv"])


if __name__ == "__main__":
    unittest.main()
