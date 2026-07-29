import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import HarnessRunState, _compiled_skill_inspection_target
from tools.context import ToolContext
from tools.registry import preflight
from tools.skills import MAX_SKILL_VIEW_PAGE_CHARS, skill_view


class _Manager:
    def __init__(self, skill_dir: Path):
        self.skill_dir = skill_dir

    def get_session_optional(self, _session_id: str) -> bool:
        return False

    def load_skill(self, *, file_path=None, **_kwargs):
        if file_path:
            return {
                "success": False,
                "error": f"unexpected fallback for {file_path}",
            }
        return {
            "success": True,
            "name": "paged-skill",
            "skill_dir": str(self.skill_dir),
        }


class SkillResourcePaginationTests(unittest.TestCase):
    def _skill(self, root: Path) -> Path:
        skill_dir = root / "paged-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: paged-skill\ndescription: paged fixture\n---\n# Skill\n",
            encoding="utf-8",
        )
        return skill_dir

    def test_unicode_pages_reassemble_exact_resource_with_stable_integrity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = self._skill(Path(temp_dir))
            resource = skill_dir / "references" / "large.md"
            resource.parent.mkdir()
            content = "开头🙂\n" + "alpha-β-数据\n" * 37 + "末尾\n"
            resource.write_text(content, encoding="utf-8")

            pages: list[dict] = []
            offset = 0
            with patch("tools.skills.get_manager", return_value=_Manager(skill_dir)):
                while True:
                    page = json.loads(asyncio.run(skill_view(
                        "paged-skill",
                        "references/large.md",
                        offset=offset,
                        limit=31,
                        user_id="u",
                        session_id="s",
                    )))
                    self.assertTrue(page["success"])
                    pages.append(page)
                    if not page["has_more"]:
                        break
                    self.assertTrue(page["truncated"])
                    self.assertGreater(page["next_offset"], offset)
                    offset = page["next_offset"]

            self.assertEqual("".join(page["content"] for page in pages), content)
            self.assertTrue(all(len(page["content"]) <= 31 for page in pages))
            self.assertEqual(
                {page["sha256"] for page in pages},
                {hashlib.sha256(content.encode("utf-8")).hexdigest()},
            )
            self.assertEqual(pages[-1]["next_offset"], None)
            self.assertFalse(pages[-1]["truncated"])
            self.assertEqual(pages[-1]["total_chars"], len(content))

    def test_offset_bounds_are_explicit_and_never_silently_clamped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = self._skill(Path(temp_dir))
            resource = skill_dir / "references" / "small.txt"
            resource.parent.mkdir()
            resource.write_text("abc", encoding="utf-8")
            with patch("tools.skills.get_manager", return_value=_Manager(skill_dir)):
                over_end = json.loads(asyncio.run(skill_view(
                    "paged-skill",
                    "references/small.txt",
                    offset=4,
                    user_id="u",
                    session_id="s",
                )))
                negative = json.loads(asyncio.run(skill_view(
                    "paged-skill",
                    "references/small.txt",
                    offset=-1,
                    user_id="u",
                    session_id="s",
                )))
                too_large = json.loads(asyncio.run(skill_view(
                    "paged-skill",
                    "references/small.txt",
                    limit=MAX_SKILL_VIEW_PAGE_CHARS + 1,
                    user_id="u",
                    session_id="s",
                )))

        self.assertEqual(over_end["reason"], "pagination_offset_out_of_range")
        self.assertEqual(negative["reason"], "invalid_pagination_offset")
        self.assertEqual(too_large["reason"], "invalid_pagination_limit")

    def test_json_escape_expansion_returns_exact_continuation_below_history_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = self._skill(Path(temp_dir))
            resource = skill_dir / "scripts" / "quoted.py"
            resource.parent.mkdir()
            content = ('print("\\\\value")\n' * 10_000) + "# end\n"
            resource.write_text(content, encoding="utf-8")
            with patch("tools.skills.get_manager", return_value=_Manager(skill_dir)):
                raw = asyncio.run(skill_view(
                    "paged-skill",
                    "scripts/quoted.py",
                    limit=MAX_SKILL_VIEW_PAGE_CHARS,
                    user_id="u",
                    session_id="s",
                ))
                first = json.loads(raw)
                second = json.loads(asyncio.run(skill_view(
                    "paged-skill",
                    "scripts/quoted.py",
                    offset=first["next_offset"],
                    limit=MAX_SKILL_VIEW_PAGE_CHARS,
                    user_id="u",
                    session_id="s",
                )))

        self.assertLess(len(raw), 50_000)
        self.assertTrue(first["has_more"])
        self.assertEqual(
            first["content"] + second["content"],
            content[:first["returned_chars"] + second["returned_chars"]],
        )

    def test_binary_resource_keeps_metadata_and_copy_guidance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = self._skill(Path(temp_dir))
            resource = skill_dir / "assets" / "fixture.bin"
            resource.parent.mkdir()
            payload = b"\x00\xffbinary\x80payload"
            resource.write_bytes(payload)
            with patch("tools.skills.get_manager", return_value=_Manager(skill_dir)):
                viewed = json.loads(asyncio.run(skill_view(
                    "paged-skill",
                    "assets/fixture.bin",
                    user_id="u",
                    session_id="s",
                )))

        self.assertTrue(viewed["success"])
        self.assertTrue(viewed["is_binary"])
        self.assertEqual(viewed["size_bytes"], len(payload))
        self.assertEqual(viewed["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertNotIn("pagination", viewed)
        self.assertIn("skill_copy_resource", viewed["hint"])

    def test_registry_schema_accepts_cursor_and_rejects_over_limit(self):
        context = ToolContext(user_id="u", session_id="s")
        accepted = preflight(
            "skill_view",
            {
                "name": "paged-skill",
                "file_path": "references/large.md",
                "offset": 100,
                "limit": 1000,
            },
            context=context,
        )
        rejected = preflight(
            "skill_view",
            {
                "name": "paged-skill",
                "file_path": "references/large.md",
                "limit": MAX_SKILL_VIEW_PAGE_CHARS + 1,
            },
            context=context,
        )
        self.assertTrue(accepted.ok)
        self.assertFalse(rejected.ok)
        self.assertIn("at most", rejected.error_json())

    def test_partial_page_is_not_a_receipt_and_next_target_uses_exact_cursor(self):
        skill_name = "paged-skill"
        path = "workers/large.yaml"
        digest = "a" * 64
        state = HarnessRunState(
            session_skill_names={skill_name},
            viewed_skill_names={skill_name},
            viewed_skill_files={skill_name: {"__manifest__"}},
            skill_available_categories={skill_name: {"workers"}},
            skill_category_files={skill_name: {"workers": [path]}},
            skill_workflow_contracts={skill_name: {"worker_files": [path]}},
        )
        first = {
            "content": "x" * 10,
            "sha256": digest,
            "pagination": {
                "offset": 0,
                "returned_chars": 10,
                "total_chars": 15,
                "has_more": True,
                "next_offset": 10,
            },
        }
        self.assertFalse(state.record_skill_view(
            {"name": skill_name, "file_path": path}, first,
        ))
        self.assertNotIn(path, state.viewed_skill_files[skill_name])
        target, error = _compiled_skill_inspection_target(
            state,
            f"inspect explicit workflow resources for session skill '{skill_name}' "
            "(pending 1 of 1 declared-resource inspection receipts)",
        )
        self.assertEqual(error, "")
        self.assertEqual(target, {
            "name": skill_name,
            "file_path": path,
            "offset": 10,
        })

        final = {
            "content": "y" * 5,
            "sha256": digest,
            "pagination": {
                "offset": 10,
                "returned_chars": 5,
                "total_chars": 15,
                "has_more": False,
                "next_offset": None,
            },
        }
        self.assertTrue(state.record_skill_view(
            {"name": skill_name, "file_path": path, "offset": 10}, final,
        ))
        self.assertIn(path, state.viewed_skill_files[skill_name])
        self.assertFalse(state.skill_resource_next_offsets)

    def test_repeated_skipped_or_changed_pages_cannot_complete_receipt(self):
        skill_name = "paged-skill"
        path = "workers/large.yaml"
        key = (skill_name, path)
        state = HarnessRunState(
            session_skill_names={skill_name},
            viewed_skill_names={skill_name},
            viewed_skill_files={skill_name: {"__manifest__"}},
            skill_category_files={skill_name: {"workers": [path]}},
            skill_workflow_contracts={skill_name: {"worker_files": [path]}},
        )

        def page(offset, returned, total, more, digest="c" * 64):
            return {
                "content": "z" * returned,
                "sha256": digest,
                "pagination": {
                    "offset": offset,
                    "returned_chars": returned,
                    "total_chars": total,
                    "has_more": more,
                    "next_offset": offset + returned if more else None,
                },
            }

        first_args = {"name": skill_name, "file_path": path}
        self.assertFalse(state.record_skill_view(
            first_args, page(0, 10, 20, True),
        ))
        self.assertFalse(state.record_skill_view(
            first_args, page(0, 10, 20, True),
        ))
        self.assertEqual(state.skill_resource_next_offsets[key], 10)

        self.assertFalse(state.record_skill_view(
            {**first_args, "offset": 15}, page(15, 5, 20, False),
        ))
        self.assertEqual(state.skill_resource_next_offsets[key], 10)
        self.assertNotIn(path, state.viewed_skill_files[skill_name])

        self.assertFalse(state.record_skill_view(
            {**first_args, "offset": 10},
            page(10, 10, 20, False, digest="d" * 64),
        ))
        self.assertIn(key, state.skill_resource_pagination_errors)
        target, error = _compiled_skill_inspection_target(
            state,
            f"inspect explicit workflow resources for session skill '{skill_name}' "
            "(pending 1 of 1 declared-resource inspection receipts)",
        )
        self.assertIsNone(target)
        self.assertIn("changed between paginated reads", error)


if __name__ == "__main__":
    unittest.main()
