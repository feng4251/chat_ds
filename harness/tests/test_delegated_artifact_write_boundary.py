import json
import unittest

from tools.context import ToolContext
from tools.registry import ToolRegistry


class DelegatedArtifactWriteBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()

        async def write_file(filepath: str, content: str) -> str:
            return json.dumps({"status": "written", "path": filepath})

        async def skill_copy_resource(
            name: str,
            source_path: str,
            destination_path: str,
        ) -> str:
            return json.dumps({
                "success": True,
                "filepath": destination_path,
            })

        async def merge_files(
            output_filepath: str,
            input_files: list[str] | None = None,
            patterns: list[str] | None = None,
        ) -> str:
            return json.dumps({
                "status": "merged",
                "path": output_filepath,
                "input_files": input_files or [],
                "patterns": patterns or [],
            })

        registry.register(
            name="write_file",
            toolset="test",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["filepath", "content"],
                },
            },
            handler=write_file,
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
        )
        registry.register(
            name="merge_files",
            toolset="test",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "output_filepath": {"type": "string"},
                        "input_files": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "patterns": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["output_filepath"],
                },
            },
            handler=merge_files,
        )
        return registry

    def _context(
        self,
        *patterns: str,
        read_paths: tuple[str, ...] = (),
    ) -> ToolContext:
        return ToolContext(
            agent_kind="delegate",
            delegated_resource_boundary=False,
            artifact_write_boundary=True,
            allowed_artifact_write_patterns=patterns,
            allowed_read_paths=read_paths,
        )

    async def test_preflight_allows_only_compiled_direct_write_paths(self):
        registry = self._registry()
        context = self._context("reports/*.md", "index.json")

        allowed = registry.preflight(
            "write_file",
            {"filepath": "reports/summary.md", "content": "complete"},
            context=context,
        )
        alias_allowed = registry.preflight(
            "write_file",
            {"filepath": "workspace/index.json", "content": "{}"},
            context=context,
        )
        undeclared = registry.preflight(
            "write_file",
            {"filepath": "scratch.py", "content": "print('x')"},
            context=context,
        )
        wrong_depth = registry.preflight(
            "write_file",
            {"filepath": "reports/nested/summary.md", "content": "x"},
            context=context,
        )

        self.assertTrue(allowed.ok, allowed.error_payload)
        self.assertTrue(alias_allowed.ok, alias_allowed.error_payload)
        self.assertFalse(undeclared.ok)
        self.assertFalse(wrong_depth.ok)
        self.assertEqual(
            "delegated_artifact_write_boundary_violation",
            undeclared.reason,
        )

    async def test_copy_destination_uses_same_compiled_write_boundary(self):
        registry = self._registry()
        context = self._context("site/index.html")

        allowed = registry.preflight(
            "skill_copy_resource",
            {
                "name": "site-builder",
                "source_path": "templates/index.html",
                "destination_path": "site/index.html",
            },
            context=context,
        )
        undeclared = registry.preflight(
            "skill_copy_resource",
            {
                "name": "site-builder",
                "source_path": "templates/index.html",
                "destination_path": "site/debug.html",
            },
            context=context,
        )

        self.assertTrue(allowed.ok, allowed.error_payload)
        self.assertFalse(undeclared.ok)

    async def test_enabled_empty_boundary_fails_closed_but_ordinary_chat_is_unchanged(self):
        registry = self._registry()
        bounded = registry.preflight(
            "write_file",
            {"filepath": "answer.md", "content": "answer"},
            context=self._context(),
        )
        ordinary = registry.preflight(
            "write_file",
            {"filepath": "answer.md", "content": "answer"},
            context=ToolContext(agent_kind="primary"),
        )

        self.assertFalse(bounded.ok)
        self.assertTrue(ordinary.ok, ordinary.error_payload)

    async def test_merge_inputs_must_be_runtime_owned_before_dispatch(self):
        registry = self._registry()
        context = self._context(
            "modules/*.md",
            "FULL_REPORT.md",
            read_paths=("evidence/verified.md",),
        )

        allowed = registry.preflight(
            "merge_files",
            {
                "output_filepath": "FULL_REPORT.md",
                "input_files": [
                    "modules/01.md",
                    "evidence/verified.md",
                ],
            },
            context=context,
        )
        undeclared_input = registry.preflight(
            "merge_files",
            {
                "output_filepath": "FULL_REPORT.md",
                "input_files": ["private/secret.md"],
            },
            context=context,
        )
        model_widened_glob = registry.preflight(
            "merge_files",
            {
                "output_filepath": "FULL_REPORT.md",
                "patterns": ["**/*.md"],
            },
            context=context,
        )
        runtime_owned_glob = registry.preflight(
            "merge_files",
            {
                "output_filepath": "FULL_REPORT.md",
                "patterns": ["modules/*.md"],
            },
            context=context,
        )

        self.assertTrue(allowed.ok, allowed.error_payload)
        self.assertFalse(undeclared_input.ok)
        self.assertFalse(model_widened_glob.ok)
        self.assertTrue(runtime_owned_glob.ok, runtime_owned_glob.error_payload)
        self.assertEqual(
            "delegated_artifact_write_boundary_violation",
            undeclared_input.reason,
        )


if __name__ == "__main__":
    unittest.main()
