import tempfile
import unittest
from pathlib import Path

from skills import scanner


SKILL = """---
name: {name}
description: test
---
instructions
"""


class SkillScannerTests(unittest.TestCase):
    def test_directory_name_mismatch_is_not_discovered_or_resolvable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir)
            try:
                user_id = "user"
                session_id = "a" * 32
                skill_dir = (
                    scanner.USER_SKILLS_BASE
                    / user_id
                    / session_id
                    / "physical-directory"
                )
                skill_dir.mkdir(parents=True)
                skill_md = skill_dir / "SKILL.md"
                skill_md.write_text(
                    SKILL.format(name="canonical-skill"), encoding="utf-8"
                )

                found = scanner.find_all_skills(user_id, session_id)
                self.assertNotIn("canonical-skill", {item["name"] for item in found})
                self.assertIsNone(scanner.resolve_skill_path(
                    "canonical-skill", user_id, session_id
                ))
                self.assertIsNone(scanner.resolve_skill_path(
                    "physical-directory", user_id, session_id
                ))
            finally:
                scanner.USER_SKILLS_BASE = old_base

    def test_declared_name_wins_over_conflicting_directory_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir)
            try:
                user_id = "user"
                session_id = "a" * 32
                session_root = scanner.USER_SKILLS_BASE / user_id / session_id
                declared = session_root / "a-package" / "canonical" / "SKILL.md"
                directory_conflict = session_root / "different" / "SKILL.md"
                declared.parent.mkdir(parents=True)
                directory_conflict.parent.mkdir(parents=True)
                declared.write_text(
                    SKILL.format(name="canonical"), encoding="utf-8"
                )
                directory_conflict.write_text(
                    SKILL.format(name="different"), encoding="utf-8"
                )

                self.assertEqual(
                    declared.resolve(),
                    scanner.resolve_skill_path("canonical", user_id, session_id),
                )
                self.assertEqual(
                    directory_conflict.resolve(),
                    scanner.resolve_skill_path("different", user_id, session_id),
                )
            finally:
                scanner.USER_SKILLS_BASE = old_base

    def test_duplicate_declared_name_has_deterministic_winner_and_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir)
            try:
                user_id = "user"
                session_id = "a" * 32
                session_root = scanner.USER_SKILLS_BASE / user_id / session_id
                first = session_root / "a-first" / "duplicate" / "SKILL.md"
                second = session_root / "b-second" / "duplicate" / "SKILL.md"
                for path in (first, second):
                    path.parent.mkdir(parents=True)
                    path.write_text(SKILL.format(name="duplicate"), encoding="utf-8")

                matches = [
                    item for item in scanner.find_all_skills(user_id, session_id)
                    if item["name"] == "duplicate"
                ]

                self.assertEqual(1, len(matches))
                self.assertEqual(first.resolve(), Path(matches[0]["path"]))
                self.assertEqual(
                    first.resolve(),
                    scanner.resolve_skill_path("duplicate", user_id, session_id),
                )
                self.assertIn(
                    "duplicate_skill_name_shadowed",
                    {item["code"] for item in matches[0]["diagnostics"]},
                )
                duplicate = next(
                    item for item in matches[0]["diagnostics"]
                    if item["code"] == "duplicate_skill_name_shadowed"
                )
                self.assertEqual(str(second.resolve()), duplicate["ignored_path"])
            finally:
                scanner.USER_SKILLS_BASE = old_base

    def test_session_does_not_see_sibling_session_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir)
            try:
                user_root = scanner.USER_SKILLS_BASE / "user"
                current = user_root / ("a" * 32)
                sibling = user_root / ("b" * 32)
                paths = {
                    user_root / "shared" / "SKILL.md": "shared",
                    user_root / "category" / "categorized" / "SKILL.md": "categorized",
                    current / "local" / "SKILL.md": "local",
                    sibling / "private" / "SKILL.md": "private",
                }
                for path, name in paths.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(SKILL.format(name=name), encoding="utf-8")

                names = {
                    item["name"]
                    for item in scanner.find_all_skills("user", "a" * 32)
                }
                self.assertIn("shared", names)
                self.assertIn("categorized", names)
                self.assertIn("local", names)
                self.assertNotIn("private", names)
                self.assertIsNone(
                    scanner.resolve_skill_path(
                        f"{'b' * 32}/private", "user", "a" * 32
                    )
                )
            finally:
                scanner.USER_SKILLS_BASE = old_base

    def test_runnable_python_is_scoped_to_exact_session_skill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir)
            try:
                user_id = "user"
                session_id = "a" * 32
                session_root = scanner.USER_SKILLS_BASE / user_id / session_id
                instruction_only = session_root / "instruction-only"
                executable = session_root / "executable"
                unrelated = session_root / "unrelated"
                for root, name in (
                    (instruction_only, "instruction-only"),
                    (executable, "executable"),
                    (unrelated, "unrelated"),
                ):
                    root.mkdir(parents=True, exist_ok=True)
                    (root / "SKILL.md").write_text(
                        SKILL.format(name=name), encoding="utf-8"
                    )
                (executable / "scripts").mkdir()
                (executable / "scripts" / "query.py").write_text(
                    "print('ok')\n", encoding="utf-8"
                )
                (executable / "scripts" / "render.sh").write_text(
                    "#!/bin/sh\nprintf ok\n", encoding="utf-8"
                )
                (executable / "scripts" / "transform.mjs").write_text(
                    "console.log('ok')\n", encoding="utf-8"
                )
                (unrelated / "helper.py").write_text(
                    "print('unrelated')\n", encoding="utf-8"
                )

                self.assertFalse(scanner.skill_has_runnable_python(
                    "instruction-only", user_id, session_id
                ))
                self.assertTrue(scanner.skill_has_runnable_python(
                    "executable", user_id, session_id
                ))
                self.assertEqual(
                    scanner.skill_runnable_script_extensions(
                        "executable", user_id, session_id
                    ),
                    frozenset({".py", ".sh", ".mjs"}),
                )
                self.assertTrue(scanner.skill_has_runnable_python(
                    "unrelated", user_id, session_id
                ))
                self.assertFalse(scanner.skill_has_runnable_python(
                    "missing", user_id, session_id
                ))
                self.assertEqual(
                    scanner.skill_runnable_script_extensions(
                        "instruction-only", user_id, session_id
                    ),
                    frozenset(),
                )
            finally:
                scanner.USER_SKILLS_BASE = old_base

    def test_user_level_python_requires_runtime_enabled_whitelist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir)
            try:
                user_id = "user"
                session_id = "a" * 32
                user_skill = scanner.USER_SKILLS_BASE / user_id / "user-skill"
                user_skill.mkdir(parents=True)
                (user_skill / "SKILL.md").write_text(
                    SKILL.format(name="user-skill"), encoding="utf-8"
                )
                (user_skill / "query.py").write_text(
                    "print('ok')\n", encoding="utf-8"
                )

                self.assertIsNotNone(scanner.resolve_skill_path(
                    "user-skill", user_id, session_id
                ))
                self.assertFalse(scanner.skill_has_runnable_python(
                    "user-skill", user_id, session_id
                ))
                self.assertTrue(scanner.skill_has_runnable_python(
                    "user-skill",
                    user_id,
                    session_id,
                    ["user-skill"],
                ))
                resources = scanner.skill_runnable_script_resources(
                    "user-skill",
                    user_id,
                    session_id,
                    ["user-skill"],
                )
                self.assertEqual("query.py", resources[0][0])
                self.assertEqual(64, len(resources[0][1]))
            finally:
                scanner.USER_SKILLS_BASE = old_base

    def test_disabled_user_duplicate_does_not_shadow_builtin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            old_user_base = scanner.USER_SKILLS_BASE
            old_builtin = scanner.BUILTIN_SKILLS_DIR
            scanner.USER_SKILLS_BASE = base / "users"
            scanner.BUILTIN_SKILLS_DIR = base / "builtin"
            try:
                user_id = "user"
                session_id = "a" * 32
                user_skill = scanner.USER_SKILLS_BASE / user_id / "portable-skill"
                builtin_skill = scanner.BUILTIN_SKILLS_DIR / "portable-skill"
                user_skill.mkdir(parents=True)
                builtin_skill.mkdir(parents=True)
                (user_skill / "SKILL.md").write_text(
                    "---\nname: portable-skill\ndescription: private disabled copy\n---\n",
                    encoding="utf-8",
                )
                (builtin_skill / "SKILL.md").write_text(
                    "---\nname: portable-skill\ndescription: shared builtin copy\n---\n",
                    encoding="utf-8",
                )

                visible = scanner.find_all_skills(
                    user_id,
                    session_id,
                    enabled_user_skills=[],
                )
                record = next(item for item in visible if item["name"] == "portable-skill")
                self.assertEqual("builtin", record["scope"])
                self.assertEqual(
                    (builtin_skill / "SKILL.md").resolve(),
                    scanner.resolve_skill_path(
                        "portable-skill",
                        user_id,
                        session_id,
                        enabled_user_skills=[],
                    ),
                )
            finally:
                scanner.USER_SKILLS_BASE = old_user_base
                scanner.BUILTIN_SKILLS_DIR = old_builtin

    def test_scanner_reads_complete_bounded_frontmatter_with_block_scalars(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir)
            try:
                user_id = "user"
                session_id = "a" * 32
                skill_dir = (
                    scanner.USER_SKILLS_BASE / user_id / session_id / "archive-review"
                )
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\n"
                    "name: archive-review\n"
                    "description: >-\n"
                    "  Review catalog records across archives.\n"
                    "  Use when reconciling collection metadata.\n"
                    "compatibility: Requires read access to the catalog export\n"
                    "metadata:\n"
                    "  author: archive-team\n"
                    "  version: \"2.0\"\n"
                    "x-long-notes: |-\n"
                    + "  bounded supporting note\n" * 250
                    + "---\n# Archive review\n",
                    encoding="utf-8",
                )

                found = scanner.find_all_skills(user_id, session_id)

                item = next(entry for entry in found if entry["name"] == "archive-review")
                self.assertEqual(
                    "Review catalog records across archives. Use when reconciling collection metadata.",
                    item["description"],
                )
            finally:
                scanner.USER_SKILLS_BASE = old_base

    def test_deep_category_tree_is_discovered_without_domain_depth_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir)
            try:
                user_id = "user"
                session_id = "a" * 32
                skill_dir = (
                    scanner.USER_SKILLS_BASE
                    / user_id
                    / session_id
                    / "organization"
                    / "division"
                    / "team"
                    / "workflows"
                    / "inventory-reconcile"
                )
                skill_dir.mkdir(parents=True)
                skill_md = skill_dir / "SKILL.md"
                skill_md.write_text(
                    SKILL.format(name="inventory-reconcile"), encoding="utf-8"
                )

                found = scanner.find_all_skills(user_id, session_id)

                self.assertIn("inventory-reconcile", {item["name"] for item in found})
                self.assertEqual(
                    skill_md.resolve(),
                    scanner.resolve_skill_path(
                        "inventory-reconcile", user_id, session_id
                    ),
                )
            finally:
                scanner.USER_SKILLS_BASE = old_base

    def test_malformed_standard_manifests_are_not_catalogued(self):
        cases = {
            "bad--name": "---\nname: bad--name\ndescription: bad name\n---\n",
            "missing-description": "---\nname: missing-description\n---\n",
            "bad-metadata": (
                "---\nname: bad-metadata\ndescription: bad metadata\n"
                "metadata:\n  owner:\n    nested: value\n---\n"
            ),
            "bad-compatibility": (
                "---\nname: bad-compatibility\ndescription: bad compatibility\n"
                f"compatibility: {'x' * 501}\n---\n"
            ),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = scanner.USER_SKILLS_BASE
            scanner.USER_SKILLS_BASE = Path(temp_dir)
            try:
                root = scanner.USER_SKILLS_BASE / "user" / ("a" * 32)
                for directory, content in cases.items():
                    skill_dir = root / directory
                    skill_dir.mkdir(parents=True)
                    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

                found = scanner.find_all_skills("user", "a" * 32)

                self.assertTrue(set(cases).isdisjoint({item["name"] for item in found}))
            finally:
                scanner.USER_SKILLS_BASE = old_base


if __name__ == "__main__":
    unittest.main()
