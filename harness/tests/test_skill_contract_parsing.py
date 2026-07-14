import unittest
from pathlib import Path

from skills.loader import load_skill_content

REPO_ROOT = Path(__file__).resolve().parents[2]


def _find_skill_md() -> Path | None:
    base = REPO_ROOT / "data" / "skills"
    if not base.is_dir():
        return None
    for md in base.rglob("healthsim-trialsim/SKILL.md"):
        return md
    return None


class SkillContractParsingTests(unittest.TestCase):
    def setUp(self):
        self.skill_md = _find_skill_md()
        if self.skill_md is None:
            self.skipTest("healthsim-trialsim skill not present under data/skills")

    def _output_contract(self) -> tuple[dict, dict]:
        res = load_skill_content(
            self.skill_md,
            skill_dir=str(self.skill_md.parent),
            session_id="default",
        )
        contract = res.get("workflow_contract") or {}
        return contract, contract.get("output_contract") or {}

    def test_declares_merge_and_final_artifact(self):
        contract, oc = self._output_contract()
        self.assertTrue(contract.get("requires_merge"))
        self.assertTrue(oc.get("merge_mandatory"))
        self.assertTrue(
            str(oc.get("declared_final_artifact", "")).endswith("_FULL_REPORT.md"),
            oc.get("declared_final_artifact"),
        )
        self.assertIn("cat", str(oc.get("merge_command", "")))

    def test_declares_file_and_section_counts(self):
        _, oc = self._output_contract()
        self.assertEqual(oc.get("declared_file_count"), 14)
        self.assertEqual(len(oc.get("declared_modular_files") or []), 11)
        self.assertEqual(oc.get("declared_section_count"), 20)

    def test_declares_skill_own_thresholds(self):
        _, oc = self._output_contract()
        self.assertEqual(oc.get("expected_min_bytes"), 153600)
        self.assertEqual(oc.get("expected_max_bytes"), 256000)
        self.assertEqual(oc.get("expected_min_lines"), 2000)
        self.assertGreaterEqual(len(oc.get("post_merge_checks") or []), 4)


if __name__ == "__main__":
    unittest.main()
