import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.file_tools import merge_files
from tools.registry import registry


class MergeFilesTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_is_registered_with_workspace_mutation_metadata(self):
        entry = registry.get_entry("merge_files")

        self.assertIsNotNone(entry)
        self.assertTrue(entry.path_scoped)
        self.assertTrue(entry.is_destructive)
        self.assertTrue(entry.mutates_workspace)
        self.assertFalse(entry.allow_in_parallel_child)
        definition = registry.get_definitions(["merge_files"])[0]["function"]
        self.assertEqual(definition["parameters"]["required"], ["output_filepath"])
        self.assertFalse(definition["parameters"]["additionalProperties"])

    async def test_explicit_inputs_keep_order_and_return_acceptance_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("tools.path_security.SANDBOX_ROOT", root):
                workspace = root / "user" / "session" / "workspace"
                workspace.mkdir(parents=True)
                (workspace / "a.md").write_text("# A\nalpha\n", encoding="utf-8")
                (workspace / "b.md").write_text("\n# B\nbeta", encoding="utf-8")

                result = json.loads(await merge_files(
                    output_filepath="reports/full.md",
                    input_files=["b.md", "a.md"],
                    separator="\n---\n",
                    user_id="user",
                    session_id="session",
                ))

                expected = "\n# B\nbeta\n---\n# A\nalpha\n"
                output = workspace / "reports" / "full.md"
                self.assertEqual(output.read_text(encoding="utf-8"), expected)
                self.assertEqual(result["status"], "merged")
                self.assertEqual(result["input_files"], ["b.md", "a.md"])
                self.assertEqual(result["input_count"], 2)
                self.assertEqual(result["bytes"], len(expected.encode("utf-8")))
                self.assertEqual(result["lines"], 6)
                self.assertEqual(result["first_nonempty_line"], "# B")
                self.assertEqual(result["last_nonempty_line"], "alpha")
                self.assertFalse(result["metadata_truncated"])

    async def test_patterns_are_sorted_and_overlapping_matches_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("tools.path_security.SANDBOX_ROOT", root):
                workspace = root / "user" / "session" / "workspace"
                modules = workspace / "modules"
                nested = modules / "nested"
                nested.mkdir(parents=True)
                (modules / "20-last.md").write_text("last", encoding="utf-8")
                (modules / "10-first.md").write_text("first", encoding="utf-8")
                (nested / "30-nested.md").write_text("nested", encoding="utf-8")

                result = json.loads(await merge_files(
                    output_filepath="full.md",
                    patterns=["modules/*.md", "modules/**/*.md"],
                    separator="|",
                    user_id="user",
                    session_id="session",
                ))

                self.assertEqual(
                    result["input_files"],
                    [
                        "modules/10-first.md",
                        "modules/20-last.md",
                        "modules/nested/30-nested.md",
                    ],
                )
                self.assertEqual(
                    (workspace / "full.md").read_text(encoding="utf-8"),
                    "first|last|nested",
                )

    async def test_output_may_not_match_input_pattern_even_before_it_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("tools.path_security.SANDBOX_ROOT", root):
                workspace = root / "user" / "session" / "workspace"
                workspace.mkdir(parents=True)
                (workspace / "part.md").write_text("part", encoding="utf-8")

                result = json.loads(await merge_files(
                    output_filepath="full.md",
                    patterns=["*.md"],
                    user_id="user",
                    session_id="session",
                ))

                self.assertEqual(result["reason"], "output_matches_input_pattern")
                self.assertEqual(result["matching_patterns"], ["*.md"])
                self.assertFalse((workspace / "full.md").exists())

    async def test_rejects_traversal_absolute_paths_and_symlink_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("tools.path_security.SANDBOX_ROOT", root):
                workspace = root / "user" / "session" / "workspace"
                workspace.mkdir(parents=True)
                outside = root / "outside.md"
                outside.write_text("secret", encoding="utf-8")
                (workspace / "link.md").symlink_to(outside)

                traversal = json.loads(await merge_files(
                    output_filepath="full.md",
                    input_files=["../outside.md"],
                    user_id="user",
                    session_id="session",
                ))
                absolute = json.loads(await merge_files(
                    output_filepath="full.md",
                    patterns=["/tmp/*.md"],
                    user_id="user",
                    session_id="session",
                ))
                symlink = json.loads(await merge_files(
                    output_filepath="full.md",
                    input_files=["link.md"],
                    user_id="user",
                    session_id="session",
                ))

                self.assertIn("Path traversal", traversal["error"])
                self.assertIn("Absolute paths", absolute["error"])
                self.assertIn("Symlinks are not allowed", symlink["error"])

    async def test_failed_atomic_replace_preserves_existing_output_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("tools.path_security.SANDBOX_ROOT", root):
                workspace = root / "user" / "session" / "workspace"
                workspace.mkdir(parents=True)
                (workspace / "part.md").write_text("new", encoding="utf-8")
                output = workspace / "full.md"
                output.write_text("old", encoding="utf-8")

                with patch("tools.file_tools.os.replace", side_effect=OSError("replace failed")):
                    result = json.loads(await merge_files(
                        output_filepath="full.md",
                        input_files=["part.md"],
                        user_id="user",
                        session_id="session",
                    ))

                self.assertIn("replace failed", result["error"])
                self.assertEqual(output.read_text(encoding="utf-8"), "old")
                self.assertEqual(list(workspace.glob(".merge_*")), [])


if __name__ == "__main__":
    unittest.main()
