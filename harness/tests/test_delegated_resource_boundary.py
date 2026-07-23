import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.context import ToolContext
from tools.delegation import _run_child
from tools.registry import ToolRegistry, delegated_resource_boundary_error


def _parent_context(
    *tools: str,
    allowed_skill_resources: tuple[tuple[str, str], ...] = (),
    allowed_skill_scripts: tuple[tuple[str, str, str], ...] = (),
    process_only_skill_scripts: tuple[tuple[str, str, str], ...] = (),
    allowed_skill_script_authorities: tuple[
        tuple[str, str, str, str, str, str], ...
    ] = (),
    allowed_skill_package_digests: tuple[tuple[str, str], ...] = (),
) -> ToolContext:
    return ToolContext(
        user_id="u",
        session_id="s",
        model_id="model",
        provider_config={
            "base_url": "http://example",
            "api_model": "model",
            "context_length": 303_872,
        },
        enabled_tools=tools,
        run_id="parent",
        root_run_id="root",
        skill_execution_resource_boundary=bool(allowed_skill_resources),
        allowed_skill_resources=allowed_skill_resources,
        allowed_skill_scripts=allowed_skill_scripts,
        process_only_skill_scripts=process_only_skill_scripts,
        allowed_skill_script_authorities=(
            allowed_skill_script_authorities
        ),
        allowed_skill_package_digests=allowed_skill_package_digests,
    )


class DelegatedRegistryResourceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()

        async def skill_view(name: str, file_path: str | None = None) -> str:
            return json.dumps({
                "success": True,
                "resource": f"{name}/{file_path or 'SKILL.md'}",
            })

        async def read_file(filepath: str) -> str:
            return json.dumps({"success": True, "resource": filepath})

        async def skill_copy_resource(
            name: str,
            source_path: str,
            destination_path: str,
        ) -> str:
            return json.dumps({
                "success": True,
                "resource": f"{name}/{source_path}",
                "filepath": destination_path,
            })

        async def run_script(script_path: str) -> str:
            return json.dumps({
                "success": True,
                "script_path": script_path,
            })

        registry.register(
            name="skill_view",
            toolset="test",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "file_path": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
            handler=skill_view,
            is_read_only=True,
        )
        for tool_name in ("run_skill_python", "run_skill_script"):
            registry.register(
                name=tool_name,
                toolset="test",
                schema={
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "script_path": {"type": "string"},
                        },
                        "required": ["script_path"],
                    },
                },
                handler=run_script,
                path_scoped=True,
            )
        registry.register(
            name="skill_copy_resource",
            toolset="test",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "source_path": {"type": "string"},
                        "destination_path": {"type": "string"},
                    },
                    "required": ["name", "source_path", "destination_path"],
                },
            },
            handler=skill_copy_resource,
            path_scoped=True,
        )
        registry.register(
            name="read_file",
            toolset="test",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {"filepath": {"type": "string"}},
                    "required": ["filepath"],
                },
            },
            handler=read_file,
            is_read_only=True,
        )
        return registry

    async def test_compiled_child_rejects_parent_manifest_and_history_browsing(self):
        registry = self._registry()
        context = ToolContext(
            run_id="child",
            agent_kind="delegate",
            delegated_resource_boundary=True,
            allowed_skill_resources=(
                ("healthsim-trialsim", "workers/evidence.yaml"),
                ("healthsim-trialsim", "therapeutic-areas/cns.md"),
                ("catalog-database", "SKILL.md"),
            ),
            allowed_read_paths=("results/intent.txt",),
        )

        manifest = json.loads(await registry.dispatch(
            "skill_view",
            {"name": "healthsim-trialsim", "file_path": "__manifest__"},
            context=context,
        ))
        historical = json.loads(await registry.dispatch(
            "read_file",
            {"filepath": "results/skill_view_oversized.txt"},
            context=context,
        ))
        undeclared_copy = json.loads(await registry.dispatch(
            "skill_copy_resource",
            {
                "name": "generic",
                "source_path": "assets/undeclared.xlsx",
                "destination_path": "deliverable.xlsx",
            },
            context=context,
        ))

        self.assertEqual(
            manifest["reason"], "delegated_resource_boundary_violation"
        )
        self.assertIn("parent Skill manifests", manifest["error"])
        self.assertEqual(
            historical["reason"], "delegated_resource_boundary_violation"
        )
        self.assertIn("historical tool-result", historical["error"])
        self.assertEqual(
            "delegated_resource_boundary_violation",
            undeclared_copy["reason"],
        )

    async def test_exact_worker_capability_resource_and_prerequisite_are_allowed(self):
        registry = self._registry()
        context = ToolContext(
            delegated_resource_boundary=True,
            allowed_skill_resources=(
                ("generic", "workers/evidence.yaml"),
                ("generic", "formats/report.md"),
                ("catalog-database", "SKILL.md"),
            ),
            allowed_read_paths=("results/prior.txt",),
        )

        calls = (
            ("skill_view", {"name": "generic", "file_path": "workers/evidence.yaml"}),
            ("skill_view", {"name": "generic", "file_path": "formats/report.md"}),
            ("skill_view", {"name": "catalog-database"}),
            (
                "skill_copy_resource",
                {
                    "name": "generic",
                    "source_path": "formats/report.md",
                    "destination_path": "report-template.md",
                },
            ),
            ("read_file", {"filepath": "results/prior.txt"}),
        )
        for tool_name, args in calls:
            with self.subTest(tool=tool_name, args=args):
                result = json.loads(
                    await registry.dispatch(tool_name, args, context=context)
                )
                self.assertTrue(result["success"])

    async def test_ordinary_context_retains_existing_reader_behavior(self):
        registry = self._registry()
        context = ToolContext(agent_kind="delegate")

        manifest = json.loads(await registry.dispatch(
            "skill_view",
            {"name": "generic", "file_path": "__manifest__"},
            context=context,
        ))
        historical = json.loads(await registry.dispatch(
            "read_file",
            {"filepath": "results/skill_view_any.txt"},
            context=context,
        ))

        self.assertTrue(manifest["success"])
        self.assertTrue(historical["success"])

    async def test_compiled_child_runner_is_limited_to_declared_skill_scripts(self):
        registry = self._registry()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = {
                ("workflow", "scripts/render.sh"): b"echo ok\n",
                ("workflow", "scripts/undeclared.sh"): b"echo no\n",
                ("catalog-database", "scripts/query.py"): b"print('ok')\n",
            }
            for (skill, relative), body in scripts.items():
                target = root / "u" / "s" / skill / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
                (root / "u" / "s" / skill / "SKILL.md").write_text(
                    f"---\nname: {skill}\ndescription: fixture\n---\n",
                    encoding="utf-8",
                )
            context = ToolContext(
                user_id="u",
                session_id="s",
                delegated_resource_boundary=True,
                allowed_skill_resources=(
                    ("workflow", "scripts/render.sh"),
                    ("catalog-database", "SKILL.md"),
                ),
                allowed_skill_scripts=(
                    (
                        "workflow",
                        "scripts/render.sh",
                        hashlib.sha256(scripts[("workflow", "scripts/render.sh")]).hexdigest(),
                    ),
                    (
                        "catalog-database",
                        "scripts/query.py",
                        hashlib.sha256(scripts[("catalog-database", "scripts/query.py")]).hexdigest(),
                    ),
                ),
            )

            with patch("skills.scanner.USER_SKILLS_BASE", root):
                allowed_calls = (
                    (
                        "run_skill_script",
                        {"script_path": "skills/workflow/scripts/render.sh"},
                    ),
                    (
                        "run_skill_python",
                        {"script_path": "skills/catalog-database/scripts/query.py"},
                    ),
                )
                for tool_name, args in allowed_calls:
                    with self.subTest(tool=tool_name, args=args):
                        result = json.loads(await registry.dispatch(
                            tool_name, args, context=context,
                        ))
                        self.assertTrue(result["success"])

                rejected_paths = (
                    "skills/workflow/scripts/undeclared.sh",
                    "skills/sibling/scripts/query.py",
                    "workspace/generated.py",
                    "scripts/query.py",
                )
                for script_path in rejected_paths:
                    with self.subTest(script_path=script_path):
                        result = json.loads(await registry.dispatch(
                            "run_skill_script",
                            {"script_path": script_path},
                            context=context,
                        ))
                        self.assertEqual(
                            "delegated_resource_boundary_violation",
                            result["reason"],
                        )

                # A post-compilation replacement is rejected by digest.
                (root / "u" / "s" / "workflow" / "scripts/render.sh").write_text(
                    "echo changed\n", encoding="utf-8"
                )
                changed = json.loads(await registry.dispatch(
                    "run_skill_script",
                    {"script_path": "skills/workflow/scripts/render.sh"},
                    context=context,
                ))
                self.assertEqual(
                    "delegated_resource_boundary_violation",
                    changed["reason"],
                )

    async def test_public_runner_schema_without_exact_grant_is_never_authority(self):
        registry = self._registry()
        for context in (None, ToolContext(agent_kind="primary")):
            with self.subTest(context=context):
                result = json.loads(await registry.dispatch(
                    "run_skill_script",
                    {"script_path": "skills/any/scripts/run.py"},
                    context=context,
                ))
                self.assertEqual(
                    "delegated_resource_boundary_violation", result["reason"]
                )
                self.assertTrue(
                    "grant" in result["error"]
                    or "capability closure" in result["error"]
                )

    async def test_enabled_user_runner_revalidates_whitelist_and_content_hash(self):
        registry = self._registry()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = b"print('promoted')\n"
            skill = root / "u" / "promoted-python"
            script = skill / "scripts" / "run.py"
            script.parent.mkdir(parents=True)
            script.write_bytes(body)
            (skill / "SKILL.md").write_text(
                "---\nname: promoted-python\ndescription: promoted fixture\n---\n",
                encoding="utf-8",
            )
            grant = (
                "promoted-python",
                "scripts/run.py",
                hashlib.sha256(body).hexdigest(),
            )
            enabled = ToolContext(
                user_id="u",
                session_id="s",
                enabled_user_skills=("promoted-python",),
                skill_execution_resource_boundary=True,
                allowed_skill_scripts=(grant,),
            )
            disabled = ToolContext(
                user_id="u",
                session_id="s",
                enabled_user_skills=(),
                skill_execution_resource_boundary=True,
                allowed_skill_scripts=(grant,),
            )

            with patch("skills.scanner.USER_SKILLS_BASE", root):
                for tool_name in ("run_skill_script", "run_skill_python"):
                    allowed = json.loads(await registry.dispatch(
                        tool_name,
                        {"script_path": "skills/promoted-python/scripts/run.py"},
                        context=enabled,
                    ))
                    denied = json.loads(await registry.dispatch(
                        tool_name,
                        {"script_path": "skills/promoted-python/scripts/run.py"},
                        context=disabled,
                    ))
                    self.assertTrue(allowed["success"])
                    self.assertEqual(
                        "delegated_resource_boundary_violation",
                        denied["reason"],
                    )

                script.write_text("print('changed')\n", encoding="utf-8")
                changed = json.loads(await registry.dispatch(
                    "run_skill_python",
                    {"script_path": "skills/promoted-python/scripts/run.py"},
                    context=enabled,
                ))

            self.assertEqual(
                "delegated_resource_boundary_violation",
                changed["reason"],
            )

    async def test_nonstandard_name_directory_mismatch_never_authorizes_script(self):
        registry = self._registry()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            physical = root / "u" / "s" / "trial-artifs-sim"
            script = physical / "scripts" / "evaluation.py"
            script.parent.mkdir(parents=True)
            body = b"print('portable')\n"
            script.write_bytes(body)
            (physical / "SKILL.md").write_text(
                "---\nname: healthsim-trialsim\ndescription: portable fixture\n---\n",
                encoding="utf-8",
            )
            context = ToolContext(
                user_id="u",
                session_id="s",
                skill_execution_resource_boundary=True,
                allowed_skill_scripts=((
                    "healthsim-trialsim",
                    "scripts/evaluation.py",
                    hashlib.sha256(body).hexdigest(),
                ),),
            )
            with patch("skills.scanner.USER_SKILLS_BASE", root):
                from skills.scanner import skill_runnable_script_resources

                inventory = skill_runnable_script_resources(
                    "healthsim-trialsim", "u", "s"
                )
                result = json.loads(await registry.dispatch(
                    "run_skill_python",
                    {
                        "script_path": (
                            "skills/healthsim-trialsim/scripts/evaluation.py"
                        )
                    },
                    context=context,
                ))
            # Standard Agent Skills require the canonical name to match the
            # immediate package directory.  The upload boundary installs ZIP
            # packages under their canonical name, so a raw mismatched tree is
            # invalid and cannot use a forged canonical grant to execute code.
            self.assertEqual((), inventory)
            self.assertIsNot(result.get("success"), True)
            self.assertIn(
                result.get("reason"),
                {"skill_script_not_found", "delegated_resource_boundary_violation"},
            )

    async def test_primary_selected_skill_boundary_closes_package_but_not_workspace(self):
        registry = self._registry()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected_body = b"print('selected')\n"
            sibling_body = b"print('sibling')\n"
            for skill, body in (
                ("math-functions", selected_body),
                ("other-functions", sibling_body),
            ):
                target = root / "u" / "s" / skill / "scripts/run.py"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
                (root / "u" / "s" / skill / "SKILL.md").write_text(
                    f"---\nname: {skill}\ndescription: fixture\n---\n",
                    encoding="utf-8",
                )
            context = ToolContext(
                user_id="u",
                session_id="s",
                skill_execution_resource_boundary=True,
                allowed_skill_resources=(
                    ("math-functions", "SKILL.md"),
                ),
                allowed_skill_scripts=((
                    "math-functions",
                    "scripts/run.py",
                    hashlib.sha256(selected_body).hexdigest(),
                ),),
            )

            with patch("skills.scanner.USER_SKILLS_BASE", root):
                selected = json.loads(await registry.dispatch(
                    "run_skill_python",
                    {"script_path": "skills/math-functions/scripts/run.py"},
                    context=context,
                ))
                sibling = json.loads(await registry.dispatch(
                    "run_skill_python",
                    {"script_path": "skills/other-functions/scripts/run.py"},
                    context=context,
                ))
                workspace_input = json.loads(await registry.dispatch(
                    "read_file",
                    {"filepath": "inputs/value.json"},
                    context=context,
                ))

            self.assertTrue(selected["success"])
            self.assertEqual(
                "delegated_resource_boundary_violation", sibling["reason"]
            )
            self.assertTrue(workspace_input["success"])

class DelegatedBoundaryPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_prompt_exposes_only_exact_intersected_capability_entrypoints(self):
        observed: dict[str, object] = {}
        first_grant = (
            "catalog-database", "scripts/catalog_helper.py", "a" * 64,
        )
        second_grant = (
            "catalog-database", "scripts/alternate_helper.py", "b" * 64,
        )
        sibling_grant = (
            "sibling-database", "scripts/private_helper.py", "c" * 64,
        )

        async def fake_dispatch(name, args, *, context):
            return json.dumps({
                "success": True,
                "content": "Capability instructions without a script filename.",
            })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed["prompt"] = messages[0]["content"]
            observed["tools"] = tools
            observed["allowed_skill_scripts"] = kwargs[
                "allowed_skill_scripts"
            ]
            yield {
                "type": "delta",
                "content": (
                    "Substantive bounded result with explicit provenance and gaps. "
                    * 20
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        def script_inventory(name, *_args, **_kwargs):
            if name == "catalog-database":
                return (
                    (first_grant[1], first_grant[2]),
                    (second_grant[1], second_grant[2]),
                )
            if name == "sibling-database":
                return ((sibling_grant[1], sibling_grant[2]),)
            return ()

        with (
            patch("tools.delegation.registry_dispatch", fake_dispatch),
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "skills.scanner.skill_runnable_script_resources",
                side_effect=script_inventory,
            ),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_entrypoint.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "Use the declared catalog capability.",
                    "skill_name": "generic-workflow",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "catalog",
                    "workflow_stage": "knowledge_bootstrap",
                    "tools": ["skill_view", "run_skill_python"],
                    "required_capability_skills": ["catalog-database"],
                },
                _parent_context(
                    "skill_view",
                    "run_skill_python",
                    allowed_skill_resources=(
                        ("catalog-database", "SKILL.md"),
                        ("catalog-database", "__manifest__"),
                        ("sibling-database", "SKILL.md"),
                    ),
                    allowed_skill_scripts=(
                        first_grant, second_grant, sibling_grant,
                    ),
                ),
                0,
            )

        self.assertEqual("completed", result["status"], result)
        self.assertEqual(
            [first_grant, second_grant],
            observed["allowed_skill_scripts"],
        )
        self.assertEqual(["run_skill_python"], observed["tools"])
        prompt = str(observed["prompt"])
        self.assertIn(
            "skills/catalog-database/scripts/catalog_helper.py", prompt
        )
        self.assertIn(
            "skills/catalog-database/scripts/alternate_helper.py", prompt
        )
        self.assertIn(f"sha256={first_grant[2]}", prompt)
        self.assertIn(f"sha256={second_grant[2]}", prompt)
        self.assertNotIn("sibling-database", prompt)
        self.assertNotIn("private_helper.py", prompt)
        self.assertNotIn("__manifest__", prompt)
        self.assertNotIn("scripts/query_drugs.py", prompt)

    async def test_child_inherits_exact_process_only_and_authority_rows(self):
        observed: dict[str, object] = {}
        base_grant = (
            "mixed-runtime", "scripts/base.cjs", "a" * 64,
        )
        browser_grant = (
            "mixed-runtime", "scripts/browser.cjs", "b" * 64,
        )
        sibling_grant = (
            "sibling-runtime", "scripts/browser.cjs", "c" * 64,
        )
        base_authority = (
            "mixed-runtime", "1" * 64, "SKILL.md", "1" * 64,
            base_grant[1], base_grant[2],
        )
        browser_authority = (
            "mixed-runtime", "1" * 64, "orchestration/main.yaml",
            "2" * 64, browser_grant[1], browser_grant[2],
        )
        sibling_authority = (
            "sibling-runtime", "3" * 64, "SKILL.md", "3" * 64,
            sibling_grant[1], sibling_grant[2],
        )

        async def fake_dispatch(name, args, *, context):
            return json.dumps({
                "success": True,
                "content": "Exact mixed runtime capability.",
            })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed["tools"] = tools
            observed["allowed_skill_scripts"] = kwargs[
                "allowed_skill_scripts"
            ]
            observed["process_only_skill_scripts"] = kwargs[
                "process_only_skill_scripts"
            ]
            observed["authorities"] = kwargs[
                "allowed_skill_script_authorities"
            ]
            observed["packages"] = kwargs[
                "allowed_skill_package_digests"
            ]
            yield {
                "type": "delta",
                "content": (
                    "Substantive bounded result with explicit provenance and "
                    "limitations. " * 20
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        def script_inventory(name, *_args, **_kwargs):
            if name == "mixed-runtime":
                return (
                    (base_grant[1], base_grant[2]),
                    (browser_grant[1], browser_grant[2]),
                )
            if name == "sibling-runtime":
                return ((sibling_grant[1], sibling_grant[2]),)
            return ()

        with (
            patch("tools.delegation.registry_dispatch", fake_dispatch),
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "skills.scanner.skill_runnable_script_resources",
                side_effect=script_inventory,
            ),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_profile.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "Use the exact mixed runtime capability.",
                    "skill_name": "generic-workflow",
                    "step_type": "worker",
                    "step_id": "mixed",
                    "workflow_stage": "worker",
                    "tools": [
                        "skill_view",
                        "run_skill_process",
                        "run_skill_script",
                    ],
                    "required_capability_skills": ["mixed-runtime"],
                },
                _parent_context(
                    "skill_view",
                    "run_skill_process",
                    "run_skill_script",
                    allowed_skill_resources=(
                        ("mixed-runtime", "SKILL.md"),
                        ("sibling-runtime", "SKILL.md"),
                    ),
                    allowed_skill_scripts=(
                        base_grant,
                        browser_grant,
                        sibling_grant,
                    ),
                    process_only_skill_scripts=(
                        browser_grant,
                        sibling_grant,
                    ),
                    allowed_skill_script_authorities=(
                        base_authority,
                        browser_authority,
                        sibling_authority,
                    ),
                    allowed_skill_package_digests=(
                        ("mixed-runtime", "4" * 64),
                        ("sibling-runtime", "5" * 64),
                    ),
                ),
                0,
            )

        self.assertEqual("completed", result["status"], result)
        self.assertEqual(
            [base_grant, browser_grant],
            observed["allowed_skill_scripts"],
        )
        self.assertEqual(
            [browser_grant],
            observed["process_only_skill_scripts"],
        )
        self.assertEqual(
            [base_authority, browser_authority],
            observed["authorities"],
        )
        self.assertEqual(
            [("mixed-runtime", "4" * 64)],
            observed["packages"],
        )

    async def test_oversize_exact_entrypoint_disclosure_fails_before_model(self):
        first_grant = (
            "catalog-database", "scripts/first.py", "a" * 64,
        )
        second_grant = (
            "catalog-database", "scripts/second.py", "b" * 64,
        )

        def script_inventory(name, *_args, **_kwargs):
            if name == "catalog-database":
                return (
                    (first_grant[1], first_grant[2]),
                    (second_grant[1], second_grant[2]),
                )
            return ()

        with (
            patch("agent_loop.run_stream") as run_stream,
            patch("tools.delegation.registry_dispatch") as dispatch,
            patch(
                "skills.scanner.skill_runnable_script_resources",
                side_effect=script_inventory,
            ),
            patch("tools.delegation._MAX_CHILD_SCRIPT_ENTRYPOINTS", 1),
            patch(
                "tools.delegation.persist_result_for_history"
            ) as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "Use the declared catalog capability.",
                    "skill_name": "generic-workflow",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "catalog",
                    "workflow_stage": "knowledge_bootstrap",
                    "tools": ["skill_view", "run_skill_python"],
                    "required_capability_skills": ["catalog-database"],
                },
                _parent_context(
                    "skill_view",
                    "run_skill_python",
                    allowed_skill_resources=((
                        "catalog-database", "SKILL.md",
                    ),),
                    allowed_skill_scripts=(first_grant, second_grant),
                ),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "prerequisite_preload_failed", result["terminal_reason"]
        )
        self.assertIn("grant count exceeds", result["error"])
        run_stream.assert_not_called()
        dispatch.assert_not_called()
        persist_result.assert_not_called()

    def test_exact_entrypoint_guidance_byte_budget_never_truncates(self):
        from tools.delegation import _render_exact_child_script_entrypoints

        grant = (
            "catalog-database", "scripts/catalog_helper.py", "a" * 64,
        )
        with patch(
            "tools.delegation._MAX_CHILD_SCRIPT_ENTRYPOINT_GUIDANCE_BYTES",
            32,
        ):
            with self.assertRaisesRegex(ValueError, "UTF-8 byte limit"):
                _render_exact_child_script_entrypoints([grant])

    async def test_compiled_worker_cannot_mint_capability_resource_authority(self):
        with (
            patch("agent_loop.run_stream") as run_stream,
            patch("tools.delegation.registry_dispatch") as dispatch,
            patch("tools.delegation.persist_result_for_history") as persist,
        ):
            result = await _run_child(
                {
                    "goal": "Use the declared helper for this workflow node.",
                    "skill_name": "generic-workflow",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "source-a",
                    "workflow_stage": "knowledge_bootstrap",
                    "tools": ["skill_view"],
                    "required_capability_skills": ["catalog-database"],
                },
                _parent_context("skill_view"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual("prerequisite_preload_failed", result["terminal_reason"])
        self.assertIn("outside the root-compiled", result["error"])
        run_stream.assert_not_called()
        dispatch.assert_not_called()
        persist.assert_not_called()

    async def test_artifact_binding_has_no_model_visible_reader_tools(self):
        observed: dict[str, object] = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed.update(kwargs)
            observed["tools"] = tools
            yield {
                "type": "delta",
                "content": (
                    "PROJECT — PASS: alpha. The request explicitly identifies Alpha "
                    "as the stable package subject, so this bounded filename component "
                    "is deterministic and contains no path or glob syntax. "
                    "artifact-plan-validation — PASS: every declared key is present, "
                    "there are no undeclared keys, and the value is a safe component.\n"
                    'INTENT_SELECTIONS_JSON: {"PROJECT":"alpha"}'
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_binding.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "Resolve the one declared artifact filename binding.",
                    "skill_name": "generic-workflow",
                    "step_type": "artifact_binding",
                    "step_id": "artifact-bindings",
                    "workflow_stage": "artifact-binding",
                    "tools": ["skill_view", "read_file", "search_files"],
                    "required_output_ids": [
                        "PROJECT", "artifact-plan-validation",
                    ],
                },
                _parent_context("skill_view", "read_file", "search_files"),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertTrue(observed["delegated_resource_boundary"])
        self.assertEqual([], observed["tools"])
        self.assertEqual([], result["model_visible_tools"])
        self.assertEqual(
            ["read_file", "search_files", "skill_view"],
            result["preloaded_reader_tools"],
        )
        self.assertEqual({"PROJECT": "alpha"}, result["intent_selections"])

    async def test_bootstrap_preloads_then_runs_with_exact_closed_allowlist(self):
        observed: dict[str, object] = {}
        boundary_checks: dict[str, str | None] = {}

        async def fake_dispatch(name, args, *, context):
            if name == "read_file":
                return json.dumps({
                    "content": "intent context",
                    "total_lines": 1,
                    "offset": 1,
                    "limit": 500,
                })
            return json.dumps({
                "success": True,
                "content": f"PRELOADED::{args['name']}::{args['file_path']}",
            })

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed.update(kwargs)
            observed["tools"] = tools
            observed["prompt"] = messages[0]["content"]
            child_context = ToolContext(
                delegated_resource_boundary=bool(
                    kwargs["delegated_resource_boundary"]
                ),
                allowed_skill_resources=tuple(
                    kwargs["allowed_skill_resources"]
                ),
                allowed_read_paths=tuple(kwargs["allowed_read_paths"]),
            )
            boundary_checks["linked"] = delegated_resource_boundary_error(
                "skill_view",
                {
                    "name": "catalog-database",
                    "file_path": "references/schema.md",
                },
                child_context,
            )
            boundary_checks["unlinked"] = delegated_resource_boundary_error(
                "skill_view",
                {
                    "name": "catalog-database",
                    "file_path": "references/unlinked.md",
                },
                child_context,
            )
            boundary_checks["other_capability"] = (
                delegated_resource_boundary_error(
                    "skill_view",
                    {"name": "other-database", "file_path": "SKILL.md"},
                    child_context,
                )
            )
            boundary_checks["traversal"] = delegated_resource_boundary_error(
                "skill_view",
                {"name": "catalog-database", "file_path": "../secret.md"},
                child_context,
            )
            yield {
                "type": "delta",
                "content": "Substantive evidence with provenance and explicit gaps. " * 20,
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("tools.delegation.registry_dispatch", fake_dispatch),
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_bootstrap.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "Build the declared evidence source.",
                    "skill_name": "generic-workflow",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "source-a",
                    "workflow_stage": "knowledge_bootstrap",
                    "tools": [
                        "skill_view",
                        "read_file",
                        "search_files",
                        "web_search",
                    ],
                    "required_result_paths": ["results/intent.txt"],
                    "required_skill_files_to_inspect": ["rules/selected.md"],
                    "required_capability_skills": ["catalog-database"],
                },
                _parent_context(
                    "skill_view",
                    "read_file",
                    "search_files",
                    "web_search",
                    allowed_skill_resources=(
                        ("generic-workflow", "rules/selected.md"),
                        ("catalog-database", "SKILL.md"),
                        ("catalog-database", "references/schema.md"),
                    ),
                ),
                0,
            )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(observed["delegated_resource_boundary"])
        self.assertEqual(
            observed["allowed_skill_resources"],
            [
                ("generic-workflow", "rules/selected.md"),
                ("catalog-database", "SKILL.md"),
                ("catalog-database", "references/schema.md"),
            ],
        )
        self.assertEqual(observed["allowed_read_paths"], ["results/intent.txt"])
        self.assertEqual(observed["tools"], ["skill_view", "web_search"])
        preload_receipt = observed["verified_preloaded_input_receipt"]
        self.assertEqual(
            {
                "version",
                "source_count",
                "kind_counts",
                "aggregate_sha256",
                "complete",
                "run_id",
                "user_id",
                "session_id",
                "workspace_scope",
            },
            set(preload_receipt),
        )
        self.assertEqual(1, preload_receipt["version"])
        self.assertTrue(preload_receipt["complete"])
        self.assertEqual(3, preload_receipt["source_count"])
        self.assertEqual(
            {"read_file": 1, "skill_view": 2},
            preload_receipt["kind_counts"],
        )
        self.assertRegex(preload_receipt["aggregate_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(observed["run_id"], preload_receipt["run_id"])
        self.assertEqual("u", preload_receipt["user_id"])
        self.assertEqual("s", preload_receipt["session_id"])
        self.assertEqual(
            observed["workspace_scope"], preload_receipt["workspace_scope"]
        )
        serialized_receipt = json.dumps(
            preload_receipt, ensure_ascii=False, sort_keys=True
        )
        for forbidden in (
            "intent context",
            "PRELOADED::",
            "results/intent.txt",
            "rules/selected.md",
            "catalog-database",
        ):
            self.assertNotIn(forbidden, serialized_receipt)
        self.assertEqual(
            result["preloaded_reader_tools"],
            ["read_file", "search_files"],
        )
        self.assertIn("PRELOADED::generic-workflow::rules/selected.md", observed["prompt"])
        self.assertIn("PRELOADED::catalog-database::SKILL.md", observed["prompt"])
        self.assertIsNone(boundary_checks["linked"])
        for key in ("unlinked", "other_capability", "traversal"):
            self.assertIn("outside the compiled task closure", boundary_checks[key])


if __name__ == "__main__":
    unittest.main()
