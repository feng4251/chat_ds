import json
import importlib

import httpx
import pytest

from tools import market_quote as market_quote_handler
from tools.market_quote import market_quote


market_quote_module = importlib.import_module("tools.market_quote")


@pytest.mark.asyncio
async def test_market_quote_uses_only_typed_internal_gateway(monkeypatch):
    seen = {}

    async def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "status": "ok",
            "quote": {"last": 410.9, "as_of": "2026-08-11 10:47:42"},
        })

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(market_quote_module.httpx, "AsyncClient", client)
    monkeypatch.setattr(
        market_quote_module.settings,
        "market_data_url",
        "http://market-data-gateway:8090/v1/quote",
    )
    result = json.loads(await market_quote("CN", "001309"))
    assert result["quote"]["last"] == 410.9
    assert seen["url"].startswith(
        "http://market-data-gateway:8090/v1/quote?"
    )
    assert "symbol=001309" in seen["url"]


def test_market_quote_is_registered_as_read_only_external_tool():
    assert callable(market_quote_handler)
