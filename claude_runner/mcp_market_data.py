#!/usr/bin/env python3
"""Stdio MCP adapter for the deployment-owned typed quote gateway."""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any


MAX_RESPONSE_BYTES = 128 * 1024
_MARKETS = frozenset({"CN", "HK", "US"})
_EXCHANGES = frozenset({"AUTO", "SZ", "SH", "BJ"})


def _gateway_url() -> str:
    endpoint = os.environ.get("CHATDS_MARKET_DATA_URL", "")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("market_data_endpoint_invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
        or parsed.path.rstrip("/") != "/v1/quote"
    ):
        raise RuntimeError("market_data_endpoint_invalid")
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        "/v1/quote",
        "",
        "",
    ))


def _quote(
    market: object,
    symbol: object,
    exchange: object = "AUTO",
) -> str:
    if not isinstance(market, str) or market.upper() not in _MARKETS:
        raise ValueError("invalid_market")
    if (
        not isinstance(symbol, str)
        or not symbol.strip()
        or len(symbol) > 12
    ):
        raise ValueError("invalid_symbol")
    if not isinstance(exchange, str) or exchange.upper() not in _EXCHANGES:
        raise ValueError("invalid_exchange")
    url = _gateway_url() + "?" + urllib.parse.urlencode({
        "market": market.upper(),
        "symbol": symbol.strip().upper(),
        "exchange": exchange.upper(),
    })
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "ChatDS-MCP/1"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError("market_data_response_too_large")
    document = json.loads(payload)
    if (
        not isinstance(document, dict)
        or document.get("status") != "ok"
        or not isinstance(document.get("quote"), dict)
    ):
        raise RuntimeError("market_data_response_invalid")
    return json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)


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
            "serverInfo": {"name": "chatds-market-data", "version": "1.0.0"},
        })
    elif method == "ping":
        _reply(identifier, {})
    elif method == "tools/list":
        _reply(identifier, {"tools": [{
            "name": "market_quote",
            "description": (
                "Get a current public near-real-time quote for a mainland "
                "China, Hong Kong, or US security, including last price, "
                "previous close, open, high, and low when the provider returns "
                "them. Use this instead of web search when the user asks for "
                "a latest/current price or previous closing price. "
                "Always report the returned source timestamp and freshness caveat."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "market": {
                        "type": "string",
                        "enum": ["CN", "HK", "US"],
                    },
                    "symbol": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 12,
                    },
                    "exchange": {
                        "type": "string",
                        "enum": ["AUTO", "SZ", "SH", "BJ"],
                        "default": "AUTO",
                        "description": "Used only for CN symbols; AUTO is normally correct.",
                    },
                },
                "required": ["market", "symbol"],
                "additionalProperties": False,
            },
        }]})
    elif method == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if name != "market_quote" or not isinstance(arguments, dict):
            _reply(identifier, error={"code": -32602, "message": "invalid_tool_call"})
            return
        try:
            content = _quote(
                arguments.get("market"),
                arguments.get("symbol"),
                arguments.get("exchange", "AUTO"),
            )
            _reply(identifier, {
                "content": [{"type": "text", "text": content}],
                "isError": False,
            })
        except Exception as exc:
            _reply(identifier, {
                "content": [{
                    "type": "text",
                    "text": f"market_quote failed: {type(exc).__name__}",
                }],
                "isError": True,
            })
    elif identifier is not None:
        _reply(identifier, error={"code": -32601, "message": "method_not_found"})


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
