"""Progressive disclosure for large session-local MCP tool catalogs."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from config import settings

BRIDGE_NAMES = {"tool_search", "tool_describe", "tool_call"}
TOKEN_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text)}


@dataclass
class DeferredCatalog:
    definitions: dict[str, dict]

    @classmethod
    def from_definitions(cls, definitions: list[dict]) -> "DeferredCatalog":
        return cls({
            item.get("function", {}).get("name", ""): item
            for item in definitions
            if item.get("function", {}).get("name")
        })

    def search(self, query: str, limit: int = 5) -> dict:
        query_tokens = _tokens(query)
        scored: list[tuple[float, str, dict]] = []
        for name, definition in self.definitions.items():
            fn = definition["function"]
            params = " ".join(
                ((fn.get("parameters") or {}).get("properties") or {}).keys()
            )
            text = f"{name.replace('_', ' ')} {fn.get('description', '')} {params}"
            doc_tokens = _tokens(text)
            overlap = len(query_tokens & doc_tokens)
            substring = 1 if query.lower() in name.lower() else 0
            score = overlap + substring * 2
            if score:
                scored.append((score, name, fn))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return {
            "matches": [{
                "name": name,
                "description": fn.get("description", ""),
            } for _, name, fn in scored[:max(1, min(limit, 20))]],
            "catalog_size": len(self.definitions),
        }

    def describe(self, name: str) -> dict:
        definition = self.definitions.get(name)
        if not definition:
            return {"error": f"Deferred tool not found: {name}"}
        return definition["function"]


def estimate_tokens(definitions: list[dict]) -> int:
    chars = sum(
        len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        for item in definitions
    )
    return math.ceil(chars / 4)


def should_defer(definitions: list[dict], context_length: int) -> bool:
    if not definitions or settings.tool_search_mode == "off":
        return False
    if settings.tool_search_mode == "on":
        return True
    threshold = max(1, int(context_length * settings.tool_search_threshold_pct / 100))
    return estimate_tokens(definitions) >= threshold


def bridge_schemas(count: int) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "tool_search",
                "description": (
                    f"Search {count} deferred MCP tools by capability. Then use "
                    "tool_describe and tool_call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tool_describe",
                "description": "Load the full parameter schema for a deferred tool.",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tool_call",
                "description": "Invoke a deferred tool by exact name.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["name", "arguments"],
                },
            },
        },
    ]
