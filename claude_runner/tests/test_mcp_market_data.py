import io
import json
import os
import unittest
from unittest.mock import patch

from claude_runner import mcp_market_data


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int):
        return self.payload[:limit]


class MarketDataMcpTests(unittest.TestCase):
    def test_quote_uses_only_typed_gateway_arguments(self):
        payload = {
            "status": "ok",
            "quote": {"last": 410.9, "as_of": "2026-08-11 10:47:42"},
        }
        with (
            patch.dict(
                os.environ,
                {"CHATDS_MARKET_DATA_URL": "http://market-data:8090/v1/quote"},
                clear=False,
            ),
            patch.object(
                mcp_market_data.urllib.request,
                "urlopen",
                return_value=_Response(payload),
            ) as opened,
        ):
            result = json.loads(mcp_market_data._quote("CN", "001309"))
        request = opened.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://market-data:8090/v1/quote?market=CN&symbol=001309&exchange=AUTO",
        )
        self.assertEqual(result["quote"]["last"], 410.9)

    def test_tools_list_exposes_only_market_quote(self):
        output = io.StringIO()
        with patch.object(mcp_market_data.sys, "stdout", output):
            mcp_market_data._handle({
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/list",
            })
        response = json.loads(output.getvalue())
        self.assertEqual(
            [tool["name"] for tool in response["result"]["tools"]],
            ["market_quote"],
        )
        description = response["result"]["tools"][0]["description"]
        self.assertIn("previous close", description)
        self.assertIn("instead of web search", description)
        self.assertFalse(
            response["result"]["tools"][0]["inputSchema"]["additionalProperties"]
        )

    def test_invalid_symbol_is_rejected_before_network(self):
        with patch.object(mcp_market_data.urllib.request, "urlopen") as opened:
            with self.assertRaisesRegex(ValueError, "invalid_symbol"):
                mcp_market_data._quote("CN", "https://attacker.invalid")
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
