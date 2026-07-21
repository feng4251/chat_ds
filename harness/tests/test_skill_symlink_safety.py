import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from skills import scanner
from skills.loader import load_skill_content
from skills.manager import SkillsManager


class SkillSymlinkSafetyTests(unittest.TestCase):
    def _write(self, root: Path, relative_path: str, content: str) -> Path:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        return path

    def _symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

    def _skill_entrypoint(self, root: Path, name: str = "safe-package") -> Path:
        return self._write(
            root,
            "SKILL.md",
            f"""
            ---
            name: {name}
            description: package boundary fixture
            ---
            # Instructions
            Use only package resources.
            """,
        )

    def test_discovery_ignores_symlink_files_and_directory_ancestors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "skill"
            outside = base / "outside"
            skill_md = self._skill_entrypoint(root)
            self._write(root, "references/nested/inside.md", "# Inside\n")
            outside_file = self._write(outside, "secret.md", "# Outside secret\n")
            outside_dir = outside / "directory"
            self._write(outside_dir, "orchestrator.yaml", "orchestrator_id: escaped\n")
            self._write(outside_dir, "reference.md", "# Escaped reference\n")
            self._symlink(root / "formats" / "escaped.md", outside_file)
            self._symlink(
                root / "workflows" / "escaped-directory",
                outside_dir,
                directory=True,
            )

            loaded = load_skill_content(skill_md, skill_dir=str(root), session_id="s1")
            linked_read = SkillsManager()._load_linked_file(
                root,
                "formats/escaped.md",
                "safe-package",
            )
            workflow_listing = SkillsManager()._load_linked_file(
                root,
                "workflows",
                "safe-package",
            )

        self.assertNotIn("error", loaded)
        manifest = json.dumps(loaded.get("linked_files") or {}, sort_keys=True)
        self.assertIn("references/nested/inside.md", manifest)
        self.assertNotIn("escaped.md", manifest)
        self.assertNotIn("escaped-directory", manifest)
        self.assertNotIn("secret.md", manifest)
        self.assertNotIn("orchestrator.yaml", manifest)
        self.assertFalse(linked_read["success"])
        self.assertEqual(linked_read.get("reason"), "symlink_resource_path")
        self.assertTrue(workflow_listing["success"])
        self.assertEqual(workflow_listing["files"], [])

    def test_worker_registry_rejects_symlink_absolute_and_traversal_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "skill"
            outside_worker = self._write(
                base / "outside",
                "outside-worker.yaml",
                "worker_id: escaped\nname: Escaped\n",
            )
            skill_md = self._skill_entrypoint(root)
            self._symlink(root / "workers" / "linked.yaml", outside_worker)
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                f"""
                orchestrator_id: boundary-orchestrator
                workers:
                  linked:
                    file: workers/linked.yaml
                  traversed:
                    file: ../outside/outside-worker.yaml
                  absolute:
                    file: {outside_worker}
                routing_rules:
                  default:
                    patterns: ["run"]
                    workers: [linked, traversed, absolute]
                """,
            )

            execution = load_skill_content(
                skill_md,
                skill_dir=str(root),
                session_id="s1",
            )["execution_contract"]

        diagnostics = execution["diagnostics"]
        codes = {item["code"] for item in diagnostics["errors"]}
        self.assertFalse(diagnostics["valid"])
        self.assertIn("symlink_worker_file_reference", codes)
        self.assertIn("unsafe_worker_file_reference", codes)
        self.assertEqual(execution.get("worker_ids") or [], [])
        self.assertNotIn(str(outside_worker), execution.get("source_files") or [])

    def test_main_skill_file_and_package_directory_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            external_root = base / "external-package"
            external_skill = self._skill_entrypoint(external_root, "external")

            file_link_root = base / "file-link-package"
            self._symlink(file_link_root / "SKILL.md", external_skill)
            file_result = load_skill_content(
                file_link_root / "SKILL.md",
                skill_dir=str(file_link_root),
                session_id="s1",
            )

            package_link = base / "package-link"
            self._symlink(package_link, external_root, directory=True)
            package_result = load_skill_content(
                package_link / "SKILL.md",
                skill_dir=str(package_link),
                session_id="s1",
            )

        self.assertIn("error", file_result)
        self.assertEqual(
            file_result["package_diagnostics"]["errors"][0]["code"],
            "symlink_resource_path",
        )
        self.assertIn("error", package_result)
        self.assertEqual(
            package_result["package_diagnostics"]["errors"][0]["code"],
            "symlink_skill_root",
        )

    def test_scanner_does_not_expose_symlinked_packages_or_entrypoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir) / "skills"
            try:
                user_id = "user"
                session_id = "a" * 32
                session_root = scanner.USER_SKILLS_BASE / user_id / session_id
                self._skill_entrypoint(session_root / "inside", "inside")

                outside_package = Path(temp_dir) / "outside-package"
                outside_skill = self._skill_entrypoint(outside_package, "outside")
                self._symlink(
                    session_root / "linked-package",
                    outside_package,
                    directory=True,
                )
                self._symlink(
                    session_root / "linked-entry" / "SKILL.md",
                    outside_skill,
                )

                found = scanner.find_all_skills(user_id, session_id)
                names = {item["name"] for item in found}
                session_items = [item for item in found if item.get("scope") == "session"]
                resolved_outside = scanner.resolve_skill_path(
                    "linked-package", user_id, session_id
                )
                resolved_entry = scanner.resolve_skill_path(
                    "linked-entry", user_id, session_id
                )
            finally:
                scanner.USER_SKILLS_BASE = old_base

        self.assertIn("inside", names)
        self.assertNotIn("outside", names)
        self.assertIsNone(resolved_outside)
        self.assertIsNone(resolved_entry)
        canonical_session = session_root.resolve()
        for item in session_items:
            Path(item["path"]).resolve().relative_to(canonical_session)

    def test_nested_regular_resources_load_compile_and_view_normally(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skill"
            skill_md = self._skill_entrypoint(root)
            self._write(root, "references/topic/deep.md", "# Deep reference\n")
            self._write(root, "scripts/helpers/run.py", "print('ok')\n")
            self._write(
                root,
                "workflows/workers/evidence.yaml",
                "worker_id: evidence\nname: Evidence Worker\n",
            )
            self._write(
                root,
                "workflows/plans/main.yaml",
                """
                orchestrator_id: nested-orchestrator
                workers:
                  evidence:
                    file: workflows/workers/evidence.yaml
                routing_rules:
                  default:
                    patterns: ["analyze"]
                    worker: evidence
                """,
            )

            loaded = load_skill_content(skill_md, skill_dir=str(root), session_id="s1")
            viewed = SkillsManager()._load_linked_file(
                root,
                "references/topic/deep.md",
                "safe-package",
            )

        self.assertNotIn("error", loaded)
        self.assertIn("references/topic/deep.md", loaded["linked_files"]["references"])
        self.assertIn("scripts/helpers/run.py", loaded["linked_files"]["scripts"])
        execution = loaded["execution_contract"]
        self.assertTrue(execution["diagnostics"]["valid"], execution["diagnostics"])
        self.assertEqual(execution["worker_ids"], ["evidence"])
        self.assertEqual(
            execution["source_files"],
            ["workflows/plans/main.yaml"],
        )
        self.assertTrue(viewed["success"])
        self.assertIn("Deep reference", viewed["content"])


if __name__ == "__main__":
    unittest.main()
