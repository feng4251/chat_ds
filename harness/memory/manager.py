"""MemoryManager — simplified memory orchestrator for chat_ds harness.

Unlike hermes-agent's multi-provider MemoryManager, this is a thin wrapper
around a single MemoryStore instance. It provides:
- Frozen snapshot at session start for system prompt injection
- Post-turn sync (background write to disk)
- Per-user isolation via user_id
"""

from __future__ import annotations

import logging
from typing import Any

from memory.scrubber import build_memory_context_block
from memory.store import MemoryStore

logger = logging.getLogger(__name__)

# Per-user store cache — one MemoryStore per user_id
_stores: dict[str, MemoryStore] = {}


def get_store(user_id: str = "default") -> MemoryStore:
    """Get or create a MemoryStore for a user."""
    if user_id not in _stores:
        store = MemoryStore(user_id=user_id)
        store.load()
        _stores[user_id] = store
    return _stores[user_id]


class MemoryManager:
    """Coordinates memory lifecycle for a single agent session.

    Usage in agent.py::

        manager = MemoryManager(user_id, session_id)
        # System prompt: manager.get_system_prompt_block()
        # Before turn:  memory_context = manager.prefetch(user_message)
        # After turn:   manager.sync(user_msg, assistant_msg)
    """

    def __init__(self, user_id: str = "default", session_id: str = "default"):
        self._user_id = user_id
        self._session_id = session_id
        self._store = get_store(user_id)

    # ── System prompt ────────────────────────────────────────────────────

    def get_system_prompt_block(self) -> str:
        """Return the frozen memory snapshot for system prompt injection.

        This is the state at session load time — mid-session writes don't
        change it. Returns empty string when neither store has entries.
        """
        return self._store.get_system_prompt_block()

    # ── Prefetch / recall ────────────────────────────────────────────────

    def prefetch(self, query: str) -> str:
        """Return memory context for injection before a turn.

        This wraps the frozen snapshot in a fenced ``<memory-context>`` block
        so the model knows it's reference material, not new user input.
        """
        block = self._store.get_system_prompt_block()
        if not block:
            return ""
        return build_memory_context_block(block)

    # ── Sync ─────────────────────────────────────────────────────────────

    def sync(
        self,
        user_content: str,
        assistant_content: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Called after each completed turn.

        For the built-in store, changes are written immediately by the memory
        tool itself (add/replace/remove call _save directly). This method is
        a no-op for the file-backed store but can be extended for additional
        post-turn processing (e.g., automatic memory extraction).
        """
        # File-backed store writes are immediate — no sync needed.
        # This hook exists for future auto-memory extraction.
        pass

    # ── Memory tool delegation ───────────────────────────────────────────

    def handle_memory_tool(
        self, action: str, target: str = "memory", content: str = "",
        old_text: str = "",
    ) -> str:
        """Route memory tool calls to the store."""
        import json

        if target not in ("memory", "user"):
            return json.dumps({"success": False, "error": f"Invalid target '{target}'."})

        if action == "add":
            if not content:
                return json.dumps({"success": False, "error": "Content required for add."})
            result = self._store.add(target, content)
        elif action == "replace":
            if not old_text:
                return json.dumps({"success": False, "error": "old_text required for replace."})
            if not content:
                return json.dumps({"success": False, "error": "content required for replace."})
            result = self._store.replace(target, old_text, content)
        elif action == "remove":
            if not old_text:
                return json.dumps({"success": False, "error": "old_text required for remove."})
            result = self._store.remove(target, old_text)
        elif action == "read":
            return json.dumps(self._store.read(), ensure_ascii=False)
        else:
            return json.dumps({"success": False, "error": f"Unknown action '{action}'."})

        return json.dumps(result, ensure_ascii=False)