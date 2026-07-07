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


if __name__ == "__main__":
    unittest.main()
