import tempfile
import unittest
from pathlib import Path

from skills.manager import SkillsManager


class SkillManagerDirectoryTests(unittest.TestCase):
    def test_linked_directory_returns_bounded_listing_instead_of_read_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir)
            (skill_dir / "protocols").mkdir()
            (skill_dir / "protocols" / "phase2.md").write_text(
                "phase two", encoding="utf-8",
            )
            (skill_dir / "protocols" / "nested").mkdir()
            (skill_dir / "protocols" / "nested" / "adaptive.yaml").write_text(
                "adaptive: true", encoding="utf-8",
            )

            result = SkillsManager()._load_linked_file(
                skill_dir,
                "protocols",
                "generic",
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["is_directory"])
        self.assertEqual(result["file"], "protocols")
        self.assertEqual(
            result["files"],
            ["protocols/nested/adaptive.yaml", "protocols/phase2.md"],
        )


if __name__ == "__main__":
    unittest.main()
