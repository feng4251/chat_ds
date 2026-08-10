#!/usr/bin/env python3
"""Small stdio MCP adapter for the deployment-owned SearXNG capability."""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any


MAX_QUERY_CHARS = 2_000
MAX_RESULTS = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _search(query: object, max_results: object = 5) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError("invalid_query")
    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= MAX_RESULTS
    ):
        raise ValueError("invalid_max_results")
    limit = max_results
    endpoint = os.environ.get("CHATDS_SEARXNG_SEARCH_URL", "")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("search_endpoint_invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
        or not parsed.path.rstrip("/").endswith("/search")
    ):
        raise RuntimeError("search_endpoint_invalid")
    url = endpoint + "?" + urllib.parse.urlencode({
        "q": query.strip(),
        "format": "json",
        "safesearch": "1",
    })
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "ChatDS-MCP/1"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError("search_response_too_large")
    document = json.loads(payload)
    rows = document.get("results") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("search_response_invalid")
    rendered: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        target = str(item.get("url") or "").strip()
        snippet = str(item.get("content") or "").strip()
        target_parts = urllib.parse.urlsplit(target)
        if (
            not title
            or target_parts.scheme not in {"http", "https"}
            or not target_parts.hostname
        ):
            continue
        rendered.append(
            f"[{len(rendered) + 1}] {title}\n{snippet[:2000]}\nURL: {target}"
        )
        if len(rendered) >= limit:
            break
    if not rendered:
        return "No search results were returned."
    unresponsive = document.get("unresponsive_engines")
    suffix = ""
    if isinstance(unresponsive, list) and unresponsive:
        names = [
            str(row[0] if isinstance(row, list) and row else row)
            for row in unresponsive[:8]
        ]
        suffix = (
            "\n\nSearch diagnostics: unresponsive engines: "
            + ", ".join(names)
        )
    return "\n\n".join(rendered) + suffix


def _reply(identifier: object, result: object = None, error: dict | None = None) -> None:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier}
    if error is None:
        response["result"] = result
    else:
        response["error"] = error
    sys.stdout.write(
        json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


def _handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    identifier = message.get("id")
    if method == "initialize":
        params = message.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        _reply(identifier, {
            "protocolVersion": requested or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "chatds-web-search", "version": "1.0.0"},
        })
    elif method == "ping":
        _reply(identifier, {})
    elif method == "tools/list":
        _reply(identifier, {"tools": [{
            "name": "web_search",
            "description": (
                "Search the current web through the deployment-owned SearXNG "
                "metasearch service. Use for current facts, news, weather, "
                "and sources outside installed Skills."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_QUERY_CHARS,
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_RESULTS,
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }]})
    elif method == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if name != "web_search" or not isinstance(arguments, dict):
            _reply(
                identifier,
                error={"code": -32602, "message": "invalid_tool_call"},
            )
            return
        try:
            content = _search(
                arguments.get("query"),
                arguments.get("max_results", 5),
            )
            _reply(identifier, {
                "content": [{"type": "text", "text": content}],
                "isError": False,
            })
        except Exception as exc:
            _reply(identifier, {
                "content": [{
                    "type": "text",
                    "text": f"web_search failed: {type(exc).__name__}",
                }],
                "isError": True,
            })
    elif identifier is not None:
        _reply(
            identifier,
            error={"code": -32601, "message": "method_not_found"},
        )


def main() -> int:
    for line in sys.stdin:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                _handle(value)
        except (ValueError, TypeError):
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
