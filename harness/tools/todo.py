"""Todo tool — in-memory task tracking per session.

Provides a todo list the agent uses to decompose complex tasks, track progress,
and maintain focus across long conversations. State is keyed by session_id.
"""

from __future__ import annotations

import json
from typing import Any

# Per-session in-memory todo stores
_stores: dict[str, TodoStore] = {}


class TodoStore:
    """In-memory todo list for one session."""

    def __init__(self):
        self._items: list[dict[str, str]] = []

    def write(self, todos: list[dict[str, Any]], merge: bool = False) -> list[dict[str, str]]:
        if not merge:
            self._items = [self._validate(t) for t in self._dedupe_by_id(todos)]
        else:
            existing = {item["id"]: item for item in self._items}
            for t in self._dedupe_by_id(todos):
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue
                if item_id in existing:
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = str(t["content"]).strip()
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in {"pending", "in_progress", "completed", "cancelled"}:
                            existing[item_id]["status"] = status
                else:
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)
            seen: set[str] = set()
            rebuilt = []
            for item in self._items:
                current = existing.get(item["id"], item)
                if current["id"] not in seen:
                    rebuilt.append(current)
                    seen.add(current["id"])
            self._items = rebuilt
        return self.read()

    def read(self) -> list[dict[str, str]]:
        return [item.copy() for item in self._items]

    @staticmethod
    def _validate(item: dict[str, Any]) -> dict[str, str]:
        item_id = str(item.get("id", "")).strip() or "?"
        content = str(item.get("content", "")).strip() or "(no description)"
        status = str(item.get("status", "pending")).strip().lower()
        if status not in {"pending", "in_progress", "completed", "cancelled"}:
            status = "pending"
        return {"id": item_id, "content": content, "status": status}

    @staticmethod
    def _dedupe_by_id(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        last_index: dict[str, int] = {}
        for i, item in enumerate(todos):
            item_id = str(item.get("id", "")).strip() or "?"
            last_index[item_id] = i
        return [todos[i] for i in sorted(last_index.values())]


def _get_store(session_id: str) -> TodoStore:
    if session_id not in _stores:
        _stores[session_id] = TodoStore()
    return _stores[session_id]


async def todo(
    todos: list[dict[str, Any]] | None = None,
    merge: bool = False,
    session_id: str = "default",
) -> str:
    """Read or write the session's todo list.

    Args:
        todos: If provided, write these items. If None, read current list.
        merge: If True, update by id. If False (default), replace entire list.
        session_id: Session identifier for isolation.

    Returns:
        JSON string with the full current list and summary metadata.
    """
    store = _get_store(session_id)

    if todos is not None:
        items = store.write(todos, merge)
    else:
        items = store.read()

    pending = sum(1 for i in items if i["status"] == "pending")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    completed = sum(1 for i in items if i["status"] == "completed")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")

    return json.dumps({
        "todos": items,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)


TODO_SCHEMA = {
    "name": "todo",
    "description": (
        "Manage your task list for the current session. Use for complex tasks "
        "with 3+ steps or when the user provides multiple tasks. "
        "Call with no parameters to read the current list.\n\n"
        "Writing:\n"
        "- Provide 'todos' array to create/update items\n"
        "- merge=false (default): replace the entire list with a fresh plan\n"
        "- merge=true: update existing items by id, add any new ones\n\n"
        "Each item: {id: string, content: string, "
        "status: pending|in_progress|completed|cancelled}\n"
        "List order is priority. Only ONE item in_progress at a time.\n"
        "Mark items completed immediately when done. If something fails, "
        "cancel it and add a revised item.\n\n"
        "Always returns the full current list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write. Omit to read current list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Unique item identifier"},
                        "content": {"type": "string", "description": "Task description"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "Current status",
                        },
                    },
                    "required": ["id", "content", "status"],
                },
            },
            "merge": {
                "type": "boolean",
                "description": "true: update existing items by id, add new ones. false (default): replace entire list.",
                "default": False,
            },
        },
        "required": [],
    },
}