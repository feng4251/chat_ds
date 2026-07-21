import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import (
    HarnessRunState,
    _active_skill_activation_failure,
    _apply_skill_activation_preflight,
    _refresh_selected_plan_activation_preflight,
    _skill_activation_preflight,
)
from skills.dependencies import (
    scan_declared_skill_dependencies,
    scan_skill_dependencies,
)
from skills.loader import load_skill_content


class SkillActivationPrerequisiteTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def test_compiler_preserves_standard_activation_fields_and_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: generic-prerequisite-skill
                description: Compile generic activation prerequisites.
                allowed-tools: [WebSearch, Bash(python:*)]
                platforms: [linux, macos]
                dependencies: [requests>=2]
                prerequisites:
                  commands: [python3]
                  env_vars:
                    - REQUIRED_TOKEN
                    - name: OPTIONAL_TOKEN
                      optional: true
                  pip: [httpx>=0.20]
                required_environment_variables:
                  - name: SECOND_TOKEN
                    optional: false
                metadata:
                  hermes:
                    prerequisites:
                      commands: [curl]
                ---
                # Generic Skill

                ## Setup
                The prose setup instructions remain visible to the model.
                """,
            )

            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        execution = loaded.get("execution_contract") or {}
        environment = execution.get("environment_contract") or {}
        diagnostics = execution.get("diagnostics") or {}
        self.assertTrue(diagnostics.get("valid"), diagnostics)
        self.assertEqual(
            ["python3", "curl"],
            [item["name"] for item in environment.get("commands") or []],
        )
        self.assertEqual(
            ["REQUIRED_TOKEN", "OPTIONAL_TOKEN", "SECOND_TOKEN"],
            [
                item["name"]
                for item in environment.get("environment_variables") or []
            ],
        )
        optional = {
            item["name"]: item["optional"]
            for item in environment["environment_variables"]
        }
        self.assertFalse(optional["REQUIRED_TOKEN"])
        self.assertTrue(optional["OPTIONAL_TOKEN"])
        self.assertEqual(
            ["requests>=2", "httpx>=0.20"],
            [item["requirement"] for item in environment.get("packages") or []],
        )
        self.assertEqual(
            ["WebSearch", "Bash(python:*)"],
            environment.get("allowed_tools"),
        )
        self.assertEqual(
            {"linux", "macos"},
            set(environment["platform_groups"][0]["allowed"]),
        )
        self.assertTrue(environment["prose_prerequisites"]["detected"])
        self.assertIn(
            "prose_prerequisites_preserved_not_inferred",
            {item["code"] for item in diagnostics.get("info") or []},
        )

    def test_explicit_empty_allowed_tools_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: no-tools
                description: Exercise an explicitly tool-free Skill.
                allowed-tools: []
                ---
                # No tools
                """,
            )
            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))
        groups = (
            (loaded.get("execution_contract") or {})
            .get("environment_contract", {})
            .get("allowed_tool_groups", [])
        )
        self.assertEqual(1, len(groups))
        self.assertTrue(groups[0]["explicit_empty"])
        self.assertEqual([], groups[0]["selectors"])

    def test_unknown_prerequisite_field_and_direct_reference_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: invalid-prerequisites
                description: Exercise invalid prerequisite declarations.
                prerequisites:
                  services: [private-daemon]
                  packages: [pkg @ https://example.invalid/pkg.whl]
                ---
                # Invalid
                """,
            )
            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))
        diagnostics = (loaded.get("execution_contract") or {}).get("diagnostics") or {}
        codes = {item["code"] for item in diagnostics.get("errors") or []}
        self.assertIn("unsupported_prerequisite_field", codes)
        self.assertIn("invalid_package_prerequisite", codes)
        self.assertFalse(diagnostics.get("valid"))

    def test_selected_worker_source_and_aggregation_prerequisites_are_retained(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: nested-prerequisites
                description: Compile nested worker and aggregation prerequisites.
                ---
                # Nested contract
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: nested
                workers:
                  worker-a:
                    role: Execute one bounded step.
                    prerequisites:
                      commands: [worker-cli]
                routing_rules:
                  run:
                    patterns: [run]
                    workers: [worker-a]
                    requires_full_output: true
                knowledge_bootstrap:
                  sources:
                    - id: source-a
                      tool: web_search
                      prerequisites:
                        env_vars: [SOURCE_TOKEN]
                aggregation:
                  steps:
                    - id: aggregate-a
                      tools: [read_file]
                      prerequisites:
                        platforms: [linux]
                output_contract:
                  artifacts: [result.json]
                """,
            )
            loaded = load_skill_content(root / "SKILL.md", skill_dir=str(root))

        execution = loaded.get("execution_contract") or {}
        diagnostics = execution.get("diagnostics") or {}
        self.assertTrue(diagnostics.get("valid"), diagnostics)
        worker = (execution.get("workers") or [])[0]
        source = execution["knowledge_bootstrap"]["sources"][0]
        step = execution["aggregation"]["steps"][0]
        self.assertEqual(
            "worker-cli",
            worker["environment_contract"]["commands"][0]["name"],
        )
        self.assertEqual(
            "SOURCE_TOKEN",
            source["environment_contract"]["environment_variables"][0]["name"],
        )
        self.assertEqual(
            ["linux"],
            step["environment_contract"]["platform_groups"][0]["allowed"],
        )

    def test_dependency_scanner_forwards_top_level_packages_to_managed_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: managed-dependencies
                description: Forward package requirements to the managed runtime.
                dependencies: [requests>=2]
                prerequisites:
                  pip: [httpx>=0.20]
                  commands: [curl]
                ---
                # Runtime
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: managed-dependencies
                workers:
                  worker-a:
                    prerequisites:
                      packages: [pydantic>=2]
                routing_rules:
                  run:
                    patterns: [run]
                    workers: [worker-a]
                """,
            )
            report = scan_skill_dependencies(root)
            package_wide = scan_declared_skill_dependencies(root)
        self.assertEqual(
            ["requests>=2", "httpx>=0.20", "pydantic>=2"],
            report.get("python_packages"),
        )
        self.assertNotIn("curl", report.get("python_packages") or [])
        self.assertEqual(
            ["requests>=2", "httpx>=0.20"],
            package_wide.get("python_packages"),
        )
        self.assertNotIn("pydantic>=2", package_wide.get("python_packages") or [])

    def test_declared_requirement_scanner_preserves_pep508_commas_and_markers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: portable-version-contract
                description: Preserve a portable structured version contract.
                ---
                # Portable dependency contract
                """,
            )
            self._write(
                root,
                "requirements.txt",
                """
                requests>=2,<3
                importlib-metadata>=6; python_version < "3.10"
                """,
            )
            report = scan_declared_skill_dependencies(root)

        self.assertEqual(
            [
                "requests>=2,<3",
                'importlib-metadata>=6; python_version < "3.10"',
            ],
            report["python_packages"],
        )

    def test_only_package_root_runtime_manifests_have_global_authority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: scoped-runtime-manifests
                description: Keep inert resources out of runtime authority.
                ---
                # Runtime manifest scopes
                """,
            )
            self._write(root, "requirements.txt", "runtime-lib==1\n")
            self._write(root, "requirements-dev.txt", "dev-only-lib==2\n")
            self._write(root, "assets/example/requirements.txt", "asset-lib==3\n")
            self._write(root, "references/requirements.txt", "reference-lib==4\n")
            self._write(root, "examples/pyproject.toml", """
                [project]
                name = "example-only"
                dependencies = ["example-lib==5"]
            """)
            report = scan_declared_skill_dependencies(root)

        self.assertEqual(["runtime-lib==1"], report["python_packages"])
        self.assertEqual(
            [{"path": "requirements.txt", "kind": "requirements"}],
            report["sources"],
        )

    def test_optional_and_development_groups_are_not_global_runtime_dependencies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: runtime-only-dependencies
                description: Select runtime rather than optional dependencies.
                ---
                # Runtime groups
                """,
            )
            self._write(root, "pyproject.toml", """
                [project]
                name = "runtime-only"
                dependencies = ["core-lib==1"]
                [project.optional-dependencies]
                test = ["test-lib==2"]
            """)
            self._write(root, "setup.cfg", """
                [options]
                install_requires =
                    setup-core==3
                [options.extras_require]
                docs =
                    docs-only==4
            """)
            self._write(root, "Pipfile", """
                [packages]
                pip-core = "==5"
                [dev-packages]
                pip-dev = "==6"
            """)
            report = scan_declared_skill_dependencies(root)

        self.assertCountEqual(
            ["core-lib==1", "setup-core==3", "pip-core==5"],
            report["python_packages"],
        )

    def test_runtime_preflight_blocks_without_reading_secret_values(self):
        workflow = {
            "execution_contract": {
                "schema_version": 1,
                "environment_contract": {
                    "commands": [
                        {"name": "available-cli", "optional": False},
                        {"name": "missing-cli", "optional": False},
                        {"name": "optional-cli", "optional": True},
                    ],
                    "environment_variables": [
                        {"name": "REQUIRED_TOKEN", "optional": False},
                        {"name": "OPTIONAL_TOKEN", "optional": True},
                    ],
                    "platform_groups": [{"allowed": ["windows"]}],
                    "packages": [
                        {"requirement": "requests>=2", "optional": False},
                    ],
                    "allowed_tools": ["WebFetch", "UnavailableAdvisory"],
                },
            }
        }
        plan = {
            "workers": {
                "worker-a": {"tools": ["WebSearch"]},
            },
            "required_workers": ["worker-a"],
            "bootstrap_sources": [],
            "aggregation_steps": [],
            "selection": "implicit",
        }
        isolated_result = {
            "valid": False,
            "checked": True,
            "execution_runtime": "isolated_skill_executor",
            "runtime_identity": {
                "execution_runtime": "isolated_skill_executor",
                "python_implementation": "cpython",
                "python_version": "3.12.1",
                "platform": "linux",
                "network": "disabled",
                "dependency_install": "disabled",
            },
            "blockers": [
                {"code": "missing_required_commands", "items": ["missing-cli"]},
                {
                    "code": "missing_required_environment_variables",
                    "items": ["REQUIRED_TOKEN"],
                },
                {
                    "code": "unsupported_runtime_platform",
                    "items": [{"current": "linux", "allowed": ["windows"]}],
                },
            ],
            "packages": {
                "requirements": ["requests>=2"],
                "status": "satisfied",
                "results": [{
                    "requirement": "requests>=2",
                    "status": "satisfied",
                    "satisfied": True,
                }],
            },
        }
        with (
            patch.dict(
                "agent_loop.os.environ",
                {"REQUIRED_TOKEN": "harness-only-secret"},
                clear=True,
            ),
            patch(
                "runtime.python_env.preflight_isolated_skill_runtime",
                return_value=isolated_result,
            ) as runtime_preflight,
        ):
            result = _skill_activation_preflight(
                workflow,
                plan,
                {"web_search", "web_extract"},
            )
        self.assertFalse(result["valid"])
        blockers = {item["code"]: item["items"] for item in result["blockers"]}
        self.assertEqual(["missing-cli"], blockers["missing_required_commands"])
        self.assertEqual(
            ["REQUIRED_TOKEN"],
            blockers["missing_required_environment_variables"],
        )
        self.assertIn("unsupported_runtime_platform", blockers)
        self.assertEqual({"WebSearch": ["web_search"]}, result["resolved_required_tools"])
        self.assertEqual(
            "satisfied",
            result["packages"]["status"],
        )
        self.assertEqual(["UnavailableAdvisory"], result["allowed_tools"]["unavailable_advisory"])
        self.assertNotIn("harness-only-secret", repr(result))
        runtime_preflight.assert_called_once_with(
            requirements=["requests>=2"],
            commands=["available-cli", "missing-cli"],
            environment_variables=["REQUIRED_TOKEN"],
            platform_groups=[{"allowed": ["windows"]}],
        )

    def test_invalid_activation_plan_is_nondispatchable_and_observable(self):
        workflow = {
            "execution_contract": {
                "schema_version": 1,
                "environment_contract": {
                    "environment_variables": [
                        {"name": "MISSING_TOKEN", "optional": False},
                    ],
                },
            }
        }
        plan = {
            "selection": "implicit",
            "route_id": "implicit-worker-graph",
            "route": {},
            "workers": {},
            "required_workers": [],
            "waves": [],
            "bootstrap_sources": [],
            "aggregation_steps": [],
        }
        with (
            patch.dict("agent_loop.os.environ", {}, clear=True),
            patch(
                "runtime.python_env.preflight_isolated_skill_runtime",
                return_value={
                    "valid": False,
                    "checked": True,
                    "blockers": [{
                        "code": "missing_required_environment_variables",
                        "items": ["MISSING_TOKEN"],
                    }],
                    "packages": {"requirements": [], "status": "not_declared"},
                },
            ),
        ):
            blocked = _apply_skill_activation_preflight(workflow, plan, set())
        self.assertEqual("invalid_contract", blocked["selection"])
        self.assertEqual([], blocked["waves"])
        self.assertFalse(blocked["activation_preflight"]["valid"])

        state = HarnessRunState(
            available_tools={"skill_view"},
            original_user_text="Run no-env-skill",
        )
        state.session_skill_names.add("no-env-skill")
        state.viewed_skill_names.add("no-env-skill")
        state.skill_execution_plans["no-env-skill"] = blocked
        failure = _active_skill_activation_failure(state)
        self.assertIsNotNone(failure)
        self.assertEqual("no-env-skill", failure["skill_name"])

    def test_only_narrowed_bash_selector_maps_to_no_shell_command_runner(self):
        workflow = {
            "execution_contract": {
                "schema_version": 1,
                "environment_contract": {
                    "allowed_tools": ["Bash(python:*)"],
                },
            }
        }
        plan = {
            "workers": {
                "analysis": {"tools": ["Bash(python:*)"]},
            },
            "required_workers": ["analysis"],
            "bootstrap_sources": [],
            "aggregation_steps": [],
        }

        result = _skill_activation_preflight(
            workflow,
            plan,
            {"run_skill_script", "run_declared_command"},
        )

        self.assertTrue(result["valid"])
        self.assertEqual(
            ["run_declared_command"],
            result["allowed_tools"]["resolved"],
        )
        self.assertEqual(
            {"Bash(python:*)": ["run_declared_command"]},
            result["allowed_tools"]["resolved_selectors"],
        )

        workflow["execution_contract"]["environment_contract"]["allowed_tools"] = ["Bash"]
        plan["workers"]["analysis"]["tools"] = ["Bash(*)"]
        rejected = _skill_activation_preflight(
            workflow, plan, {"run_declared_command"}
        )
        self.assertFalse(rejected["valid"])
        blockers = {item["code"]: item["items"] for item in rejected["blockers"]}
        self.assertEqual(["Bash(*)"], blockers["missing_declared_step_tools"])
        self.assertIn("Bash", rejected["allowed_tools"]["unavailable_advisory"])

    def test_rich_tool_descriptors_do_not_turn_metadata_keys_into_requirements(self):
        workflow = {
            "execution_contract": {
                "schema_version": 1,
                "environment_contract": {},
            }
        }
        plan = {
            "workers": {
                "analysis": {
                    "tools": [
                        {
                            "name": "fda_search",
                            "source": "skill:fda-database",
                            "description": "Capability Skill lookup",
                        },
                        {
                            "name": "domain_knowledge",
                            "source": "project",
                            "paths": ["references/domain.md"],
                            "description": "Local compiled resource",
                        },
                        {
                            "name": "web_search",
                            "source": "WebSearch",
                            "description": "Native web lookup",
                        },
                        {
                            "name": "web_fetch",
                            "source": "via Bash/WebFetch",
                            "usage": "Fetch one declared URL",
                        },
                    ],
                },
            },
            "required_workers": ["analysis"],
            "bootstrap_sources": [],
            "aggregation_steps": [],
        }

        result = _skill_activation_preflight(
            workflow,
            plan,
            {"web_search", "web_extract"},
        )

        self.assertTrue(result["valid"], result)
        self.assertEqual(
            {"WebSearch": ["web_search"], "WebFetch": ["web_extract"]},
            result["resolved_required_tools"],
        )
        self.assertNotIn("name", result["resolved_required_tools"])
        self.assertNotIn("description", result["resolved_required_tools"])

    def test_step_descriptor_identity_and_paths_are_not_tool_authority(self):
        workflow = {
            "execution_contract": {
                "schema_version": 1,
                "environment_contract": {},
            }
        }
        plan = {
            "workers": {
                "analysis": {
                    "tools": [
                        {
                            "name": "web_search",
                            "id": "web_extract",
                            "path": "write_file",
                            "source": "project",
                        },
                        {
                            "name": "web_search",
                            "source": "documentation mentions WebSearch",
                        },
                    ],
                },
            },
            "required_workers": ["analysis"],
            "bootstrap_sources": [],
            "aggregation_steps": [],
        }

        result = _skill_activation_preflight(
            workflow,
            plan,
            {"web_search", "web_extract", "write_file"},
        )

        self.assertTrue(result["valid"], result)
        self.assertEqual({}, result["resolved_required_tools"])
        self.assertFalse(any(
            blocker.get("code") == "missing_declared_step_tools"
            for blocker in result["blockers"]
        ))

    def test_selected_intent_route_refreshes_pending_preflight(self):
        workflow = {
            "execution_contract": {
                "schema_version": 1,
                "environment_contract": {},
            },
        }
        stale_preflight = {
            "valid": True,
            "checked": False,
            "blockers": [],
            "resolved_required_tools": {},
        }
        state = HarnessRunState()
        state.skill_execution_plans["routed-skill"] = {
            "selection": "intent_route_mapped",
            "route_id": "full",
            "route": {},
            "workers": {
                "research": {
                    "tools": ["WebSearch", "WebFetch"],
                },
            },
            "required_workers": ["research"],
            "waves": [{
                "id": "research",
                "mode": "sequential",
                "workers": ["research"],
                "dependencies": [],
            }],
            "bootstrap_sources": [],
            "aggregation_steps": [],
            "requires_full_output": True,
            "requires_artifact_output": True,
            "output_contract": {"declared_final_artifact": "report.md"},
            "activation_preflight": stale_preflight,
        }
        state.skill_artifact_plans["routed-skill"] = {"valid": True}

        with patch(
            "runtime.python_env.preflight_isolated_skill_runtime",
            return_value={
                "valid": True,
                "checked": False,
                "runtime_identity": {"platform": "linux"},
                "blockers": [],
                "packages": {"requirements": [], "status": "not_declared"},
            },
        ):
            refreshed, valid = _refresh_selected_plan_activation_preflight(
                state,
                "routed-skill",
                workflow,
                {"web_search", "web_extract"},
            )

        self.assertTrue(valid)
        self.assertTrue(refreshed["activation_preflight"]["checked"])
        self.assertEqual(
            {
                "WebSearch": ["web_search"],
                "WebFetch": ["web_extract"],
            },
            refreshed["activation_preflight"]["resolved_required_tools"],
        )
        self.assertIs(
            refreshed,
            state.skill_execution_plans["routed-skill"],
        )
        self.assertIn("routed-skill", state.skill_artifact_plans)

    def test_selected_intent_route_preflight_failure_clears_artifact_state(self):
        workflow = {
            "execution_contract": {
                "schema_version": 1,
                "environment_contract": {},
            },
        }
        state = HarnessRunState(
            skill_workflow_activation="explicit_skill_request",
            original_user_text="Run routed-skill and create the full report.",
        )
        state.session_skill_names.add("routed-skill")
        state.viewed_skill_names.add("routed-skill")
        state.skill_execution_plans["routed-skill"] = {
            "selection": "intent_route_mapped",
            "route_id": "full",
            "route": {},
            "workers": {
                "research": {
                    "tools": ["WebSearch", "WebFetch"],
                },
            },
            "required_workers": ["research"],
            "waves": [{
                "id": "research",
                "mode": "sequential",
                "workers": ["research"],
                "dependencies": [],
            }],
            "bootstrap_sources": [],
            "aggregation_steps": [],
            "requires_full_output": True,
            "requires_artifact_output": True,
            "output_contract": {"declared_final_artifact": "report.md"},
            "activation_preflight": {
                "valid": True,
                "checked": False,
                "blockers": [],
                "resolved_required_tools": {},
            },
        }
        state.skill_artifact_plans["routed-skill"] = {"valid": True}
        state.skill_artifact_bindings["routed-skill"] = {"NAME": "report"}
        state.skill_artifact_binding_results["routed-skill"] = {
            "status": "completed",
        }
        state.skill_completed_artifact_binding.add("routed-skill")
        state.skill_failed_artifact_binding["routed-skill"] = "stale"

        with patch(
            "runtime.python_env.preflight_isolated_skill_runtime",
            return_value={
                "valid": True,
                "checked": False,
                "runtime_identity": {"platform": "linux"},
                "blockers": [],
                "packages": {"requirements": [], "status": "not_declared"},
            },
        ):
            refreshed, valid = _refresh_selected_plan_activation_preflight(
                state,
                "routed-skill",
                workflow,
                # The final selected boundary lacks the route's WebFetch
                # requirement and must fail closed.
                {"web_search"},
            )

        self.assertFalse(valid)
        self.assertEqual("invalid_contract", refreshed["selection"])
        self.assertEqual([], refreshed["required_workers"])
        self.assertEqual([], refreshed["waves"])
        blockers = {
            item["code"]: item.get("items")
            for item in refreshed["activation_preflight"]["blockers"]
        }
        self.assertEqual(
            ["WebFetch"],
            blockers["missing_declared_step_tools"],
        )
        self.assertNotIn("routed-skill", state.skill_artifact_plans)
        self.assertNotIn("routed-skill", state.skill_artifact_bindings)
        self.assertNotIn("routed-skill", state.skill_artifact_binding_results)
        self.assertNotIn(
            "routed-skill",
            state.skill_completed_artifact_binding,
        )
        self.assertNotIn("routed-skill", state.skill_failed_artifact_binding)
        self.assertIsNotNone(_active_skill_activation_failure(state))


if __name__ == "__main__":
    unittest.main()
