from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.context import ToolContext
from tools.tool_result_reader import read_tool_result
from tools.tool_result_storage import (
    TOOL_RESULT_HANDLE_PREFIX,
    persist_tool_result_spill,
    wrap_result_with_receipt,
)


class ToolResultReadbackTests(unittest.IsolatedAsyncioTestCase):
    def _workspace(self, root: Path) -> None:
        (root / "u" / "s" / "workspace").mkdir(parents=True)

    async def test_lossless_spill_supports_bounded_character_queries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._workspace(root)
            value = "prefix:" + ("甲" * 30_000) + ":needle:tail"
            with patch("tools.path_security.SANDBOX_ROOT", root):
                handle = persist_tool_result_spill(
                    value,
                    "sample",
                    user_id="u",
                    session_id="s",
                )
                self.assertIsNotNone(handle)
                context = ToolContext(
                    user_id="u",
                    session_id="s",
                    allowed_tool_result_handles=(str(handle),),
                )
                head = json.loads(await read_tool_result(
                    str(handle), offset=0, limit=12, context=context
                ))
                match = json.loads(await read_tool_result(
                    str(handle), pattern="needle", limit=11, context=context
                ))
                tail = json.loads(await read_tool_result(
                    str(handle), from_end=True, limit=4, context=context
                ))

            self.assertEqual("prefix:" + ("甲" * 5), head["content"])
            self.assertEqual("needle:tail", match["content"])
            self.assertEqual("tail", tail["content"])
            self.assertTrue(head["has_more_after"])
            self.assertGreater(match["match_offset"], 20_000)

    async def test_forged_cross_run_and_symlink_handles_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._workspace(root)
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            outside.chmod(0o600)
            results = root / "u" / "s" / "results"
            results.mkdir()
            (results / "linked.txt").symlink_to(outside)
            (results / "hardlinked.txt").hardlink_to(outside)
            linked = TOOL_RESULT_HANDLE_PREFIX + "linked.txt"
            hardlinked = TOOL_RESULT_HANDLE_PREFIX + "hardlinked.txt"
            with patch("tools.path_security.SANDBOX_ROOT", root):
                forged = json.loads(await read_tool_result(
                    TOOL_RESULT_HANDLE_PREFIX + "forged.txt",
                    context=ToolContext(user_id="u", session_id="s"),
                ))
                unsafe = json.loads(await read_tool_result(
                    linked,
                    context=ToolContext(
                        user_id="u",
                        session_id="s",
                        allowed_tool_result_handles=(linked,),
                    ),
                ))
                multiply_linked = json.loads(await read_tool_result(
                    hardlinked,
                    context=ToolContext(
                        user_id="u",
                        session_id="s",
                        allowed_tool_result_handles=(hardlinked,),
                    ),
                ))

            self.assertEqual(
                "tool_result_handle_not_granted", forged["error_code"]
            )
            self.assertEqual("tool_result_unavailable", unsafe["error_code"])
            self.assertEqual(
                "tool_result_unavailable",
                multiply_linked["error_code"],
            )

    def test_overflow_retries_get_distinct_handles_and_bounded_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._workspace(root)
            raw = "z" * 60_000
            with patch("tools.path_security.SANDBOX_ROOT", root):
                first, first_handle = wrap_result_with_receipt(
                    raw, "generic", user_id="u", session_id="s"
                )
                second, second_handle = wrap_result_with_receipt(
                    raw, "generic", user_id="u", session_id="s"
                )

            self.assertIsNotNone(first_handle)
            self.assertIsNotNone(second_handle)
            self.assertNotEqual(first_handle, second_handle)
            self.assertIn("read_tool_result", first)
            self.assertLessEqual(len(first), 52_000)


if __name__ == "__main__":
    unittest.main()
