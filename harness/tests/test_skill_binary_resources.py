import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills.loader import load_skill_content
from skills.manager import SkillsManager
from tools.context import ToolContext
from tools.delegation import _verified_artifact_receipts
from tools.skills import skill_copy_resource
from workspace_context import get_workspace


class _Manager:
    def __init__(self, skill_dir: Path):
        self.skill_dir = skill_dir

    def get_session_optional(self, _session_id: str) -> bool:
        return False

    def load_skill(self, **_kwargs):
        return {
            "success": True,
            "name": "binary-fixture",
            "skill_dir": str(self.skill_dir),
        }


class SkillBinaryResourceTests(unittest.TestCase):
    def test_binary_resource_metadata_and_atomic_workspace_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "binary-fixture"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: binary-fixture\ndescription: Exercise binary Skill resource handling.\n---\nUse assets.\n",
                encoding="utf-8",
            )
            asset = skill_dir / "templates" / "model.xlsx"
            asset.parent.mkdir()
            payload = b"PK\x03\x04\x00\xffbinary-workbook"
            asset.write_bytes(payload)

            loaded = load_skill_content(
                skill_dir / "SKILL.md",
                skill_dir=str(skill_dir),
                session_id="s",
            )
            self.assertIn(
                "templates/model.xlsx",
                (loaded.get("linked_files") or {}).get("templates") or [],
            )

            viewed = SkillsManager()._load_linked_file(
                skill_dir,
                "templates/model.xlsx",
                "binary-fixture",
            )
            self.assertTrue(viewed["success"])
            self.assertTrue(viewed["is_binary"])
            self.assertEqual(len(payload), viewed["size_bytes"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), viewed["sha256"])
            self.assertIn("skill_copy_resource", viewed["hint"])

            workspace_root = root / "sessions"
            with (
                patch("tools.skills.get_manager", return_value=_Manager(skill_dir)),
                patch("tools.path_security.SANDBOX_ROOT", workspace_root),
            ):
                copied = json.loads(
                    asyncio.run(
                        skill_copy_resource(
                            name="binary-fixture",
                            source_path="templates/model.xlsx",
                            destination_path="deliverables/model.xlsx",
                            user_id="u",
                            session_id="s",
                        )
                    )
                )
                duplicate = json.loads(
                    asyncio.run(
                        skill_copy_resource(
                            name="binary-fixture",
                            source_path="templates/model.xlsx",
                            destination_path="deliverables/model.xlsx",
                            user_id="u",
                            session_id="s",
                        )
                    )
                )

            destination = workspace_root / "u" / "s" / "workspace" / "deliverables" / "model.xlsx"
            self.assertTrue(copied["success"])
            self.assertEqual("skill_copy_resource", copied["source_tool"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), copied["sha256"])
            self.assertEqual(payload, destination.read_bytes())
            self.assertFalse(duplicate["success"])
            self.assertIn("already exists", duplicate["error"])

    def test_copy_rejects_source_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("# fixture\n", encoding="utf-8")
            with (
                patch("tools.skills.get_manager", return_value=_Manager(skill_dir)),
                patch("tools.path_security.SANDBOX_ROOT", root / "sessions"),
            ):
                result = json.loads(
                    asyncio.run(
                        skill_copy_resource(
                            name="binary-fixture",
                            source_path="../outside.bin",
                            destination_path="copy.bin",
                            user_id="u",
                            session_id="s",
                        )
                    )
                )
            self.assertFalse(result["success"])
            self.assertEqual("traversal_resource_path", result["reason"])

    def test_copy_and_script_outputs_are_verified_as_typed_artifact_receipts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("workspace_context.WORKSPACE_ROOT", root):
                workspace = get_workspace("u", "s")
                copied = workspace / "package.xlsx"
                generated = workspace / "chart.png"
                copied.write_bytes(b"copied-binary")
                generated.write_bytes(b"generated-binary")
                copied_hash = hashlib.sha256(copied.read_bytes()).hexdigest()
                generated_hash = hashlib.sha256(generated.read_bytes()).hexdigest()
                receipts = _verified_artifact_receipts(
                    [
                        (
                            "skill_copy_resource",
                            "copy-call",
                            {"destination_path": "package.xlsx"},
                            [{
                                "path": "package.xlsx",
                                "size_bytes": copied.stat().st_size,
                                "sha256": copied_hash,
                            }],
                        ),
                        (
                            "run_skill_script",
                            "script-call",
                            {"script_path": "skills/generic/scripts/render.sh"},
                            [{
                                "path": "chart.png",
                                "size_bytes": generated.stat().st_size,
                                "sha256": generated_hash,
                            }],
                        ),
                    ],
                    ToolContext(user_id="u", session_id="s"),
                )

            self.assertEqual(
                ["package.xlsx", "chart.png"],
                [receipt["path"] for receipt in receipts],
            )
            self.assertEqual(copied_hash, receipts[0]["sha256"])
            self.assertEqual(generated_hash, receipts[1]["sha256"])


if __name__ == "__main__":
    unittest.main()
