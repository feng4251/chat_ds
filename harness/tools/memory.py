"""Memory tool — persistent curated memory across sessions.

Two stores:
  - MEMORY.md: agent's personal notes and observations
  - USER.md: what the agent knows about the user

Actions: add, replace, remove, read.
"""

from __future__ import annotations

import json
from typing import Any

from memory.manager import get_store


async def memory(
    action: str,
    target: str = "memory",
    content: str = "",
    old_text: str = "",
    session_id: str = "default",
) -> str:
    """Persistent memory tool.

    Args:
        action: One of 'add', 'replace', 'remove', 'read'.
        target: 'memory' for agent notes, 'user' for user profile.
        content: Entry content (required for add/replace).
        old_text: Substring identifying the entry to replace/remove.
        session_id: Session identifier (used to derive user_id mapping).

    Returns:
        JSON result with success status and current state.
    """
    # session_id doubles as user_id for now; can be mapped separately later
    store = get_store(session_id)

    if target not in ("memory", "user"):
        return json.dumps({"success": False, "error": f"Invalid target '{target}'. Use 'memory' or 'user'."})

    if action == "add":
        if not content:
            return json.dumps({"success": False, "error": "content is required for 'add' action."})
        result = store.add(target, content)

    elif action == "replace":
        if not old_text:
            return json.dumps({"success": False, "error": "old_text is required for 'replace' action."})
        if not content:
            return json.dumps({"success": False, "error": "content is required for 'replace' action."})
        result = store.replace(target, old_text, content)

    elif action == "remove":
        if not old_text:
            return json.dumps({"success": False, "error": "old_text is required for 'remove' action."})
        result = store.remove(target, old_text)

    elif action == "read":
        return json.dumps(store.read(), ensure_ascii=False)

    else:
        return json.dumps({"success": False, "error": f"Unknown action '{action}'. Use: add, replace, remove, read."})

    return json.dumps(result, ensure_ascii=False)


MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable information to persistent memory that survives across sessions. "
        "Memory is injected into future turns, so keep it compact and focused on facts "
        "that will still matter later.\n\n"
        "WHEN TO SAVE (proactively, don't wait to be asked):\n"
        "- User corrects you or says 'remember this' / 'don't do that again'\n"
        "- User shares a preference, habit, or personal detail\n"
        "- You discover environment facts (OS, tools, project structure)\n"
        "- You learn conventions, API quirks, or workflows\n"
        "- You identify stable facts useful in future sessions\n\n"
        "PRIORITY: User preferences/corrections > environment facts > procedural knowledge.\n\n"
        "Do NOT save task progress, session outcomes, or temporary TODO state.\n\n"
        "TWO TARGETS:\n"
        "- 'user': who the user is — name, role, preferences, communication style\n"
        "- 'memory': your notes — environment facts, project conventions, lessons learned\n\n"
        "ACTIONS: add (new entry), replace (update existing), remove (delete), read (list all)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove", "read"],
                "description": "The action to perform.",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which store: 'memory' for notes, 'user' for user profile.",
            },
            "content": {
                "type": "string",
                "description": "Entry content. Required for 'add' and 'replace'.",
            },
            "old_text": {
                "type": "string",
                "description": "Short unique substring identifying the entry to replace or remove.",
            },
        },
        "required": ["action", "target"],
    },
}