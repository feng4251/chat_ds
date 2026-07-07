"""MemoryStore — bounded, file-backed persistent memory per user.

Two stores:
  - MEMORY.md: agent's personal notes and observations
  - USER.md: what the agent knows about the user

Frozen snapshot pattern: system prompt snapshot is captured at session start
and never mutated mid-session (preserves prefix cache). Mid-session writes
update files on disk immediately but don't change the system prompt.

Entry delimiter: § (section sign). Entries can be multiline.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ENTRY_DELIMITER = "\n§\n"
DATA_DIR = Path("data/memories")


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=".mem_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class MemoryStore:
    """Bounded curated memory with file persistence per user.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls,
        persisted to disk. Tool responses always reflect this live state.
    """

    def __init__(
        self,
        user_id: str = "default",
        memory_char_limit: int = 2200,
        user_char_limit: int = 1375,
        data_dir: Optional[Path] = None,
    ):
        self._user_id = user_id
        self._data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": ""}

    # ── Path helpers ─────────────────────────────────────────────────────

    @property
    def _mem_dir(self) -> Path:
        return self._data_dir / self._user_id

    def _path_for(self, target: str) -> Path:
        if target == "user":
            return self._mem_dir / "USER.md"
        return self._mem_dir / "MEMORY.md"

    # ── Load ─────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load entries from disk and capture frozen system prompt snapshot."""
        self._mem_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(self._path_for("memory"))
        self.user_entries = self._read_file(self._path_for("user"))

        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }

    # ── System prompt snapshot ───────────────────────────────────────────

    def get_system_prompt_block(self) -> str:
        """Return the frozen snapshot for system prompt injection.

        Returns empty string if both stores are empty at load time.
        """
        blocks = []
        for target in ("memory", "user"):
            block = self._system_prompt_snapshot.get(target, "")
            if block:
                blocks.append(block)
        return "\n\n".join(blocks)

    # ── Read (tool-facing) ───────────────────────────────────────────────

    def read(self) -> dict:
        """Return live state of both stores for the memory tool."""
        return {
            "memory": {
                "entries": list(self.memory_entries),
                "usage": self._usage_str("memory"),
            },
            "user": {
                "entries": list(self.user_entries),
                "usage": self._usage_str("user"),
            },
        }

    # ── Add ──────────────────────────────────────────────────────────────

    def add(self, target: str, content: str) -> dict:
        """Append a new entry."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        entries = self._entries_for(target)
        limit = self._char_limit(target)

        if content in entries:
            return self._success_response(target, "Entry already exists (no duplicate added).")

        new_total = len(ENTRY_DELIMITER.join(entries + [content]))
        if new_total > limit:
            current = self._char_count(target)
            return {
                "success": False,
                "error": (
                    f"Memory at {current:,}/{limit:,} chars. "
                    f"Adding this entry ({len(content)} chars) would exceed the limit. "
                    f"Replace or remove existing entries first."
                ),
                "current_entries": entries,
                "usage": f"{current:,}/{limit:,}",
            }

        entries.append(content)
        self._set_entries(target, entries)
        self._save(target)
        return self._success_response(target, "Entry added.")

    # ── Replace ──────────────────────────────────────────────────────────

    def replace(self, target: str, old_text: str, new_content: str) -> dict:
        """Find entry containing old_text substring and replace it."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use remove to delete."}

        entries = self._entries_for(target)
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text}'."}

        if len(matches) > 1:
            unique_texts = {e for _, e in matches}
            if len(unique_texts) > 1:
                previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": previews,
                }

        idx = matches[0][0]
        limit = self._char_limit(target)

        test_entries = entries.copy()
        test_entries[idx] = new_content
        new_total = len(ENTRY_DELIMITER.join(test_entries))
        if new_total > limit:
            return {
                "success": False,
                "error": (
                    f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                    f"Shorten the new content or remove other entries first."
                ),
            }

        entries[idx] = new_content
        self._set_entries(target, entries)
        self._save(target)
        return self._success_response(target, "Entry replaced.")

    # ── Remove ───────────────────────────────────────────────────────────

    def remove(self, target: str, old_text: str) -> dict:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        entries = self._entries_for(target)
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text}'."}

        if len(matches) > 1:
            unique_texts = {e for _, e in matches}
            if len(unique_texts) > 1:
                previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": previews,
                }

        idx = matches[0][0]
        entries.pop(idx)
        self._set_entries(target, entries)
        self._save(target)
        return self._success_response(target, "Entry removed.")

    # ── Internal helpers ─────────────────────────────────────────────────

    def _entries_for(self, target: str) -> list[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        return len(ENTRY_DELIMITER.join(entries)) if entries else 0

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def _save(self, target: str) -> None:
        """Persist entries to disk."""
        self._mem_dir.mkdir(parents=True, exist_ok=True)
        content = ENTRY_DELIMITER.join(self._entries_for(target))
        _atomic_write(self._path_for(target), content)

    def _usage_str(self, target: str) -> str:
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        return f"{pct}% — {current:,}/{limit:,} chars"

    def _success_response(self, target: str, message: str = "") -> dict:
        entries = self._entries_for(target)
        resp = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": self._usage_str(target),
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: list[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []
        if not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]