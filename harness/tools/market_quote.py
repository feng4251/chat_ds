"""Engine-independent client for the deployment-owned quote gateway."""

from __future__ import annotations

import json
from typing import Any

import httpx

from config import settings


MARKET_QUOTE_SCHEMA = {
    "name": "market_quote",
    "description": (
        "Get a current public near-real-time quote for a mainland China, "
        "Hong Kong, or US security, including the previous close when "
        "available. Prefer this over web search for current or previous "
        "closing prices."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "market": {"type": "string", "enum": ["CN", "HK", "US"]},
            "symbol": {"type": "string", "minLength": 1, "maxLength": 12},
            "exchange": {
                "type": "string",
                "enum": ["AUTO", "SZ", "SH", "BJ"],
                "default": "AUTO",
            },
        },
        "required": ["market", "symbol"],
        "additionalProperties": False,
    },
}


async def market_quote(
    market: str,
    symbol: str,
    exchange: str = "AUTO",
) -> str:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                settings.market_data_url,
                params={
                    "market": market,
                    "symbol": symbol,
                    "exchange": exchange,
                },
                headers={"Accept": "application/json"},
            )
        response.raise_for_status()
        payload: Any = response.json()
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "ok"
            or not isinstance(payload.get("quote"), dict)
        ):
            raise ValueError("market_data_response_invalid")
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "failure_kind": "market_data_unavailable",
            "error": type(exc).__name__,
        }, ensure_ascii=False)
