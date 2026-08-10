import io
import json
import os
import unittest
from unittest.mock import patch

from claude_runner import mcp_web_search


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int):
        return self.payload[:limit]


class WebSearchMcpTests(unittest.TestCase):
    def test_search_returns_bounded_normalized_results(self):
        payload = {
            "results": [
                {
                    "title": "Current result",
                    "url": "https://news.example.test/item",
                    "content": "fresh snippet",
                },
                {"title": "unsafe", "url": "file:///etc/passwd"},
            ],
            "unresponsive_engines": [["engine-x", "timeout"]],
        }
        with (
            patch.dict(
                os.environ,
                {"CHATDS_SEARXNG_SEARCH_URL": "http://search.internal:8080/search"},
                clear=False,
            ),
            patch.object(
                mcp_web_search.urllib.request,
                "urlopen",
                return_value=_Response(payload),
            ) as opened,
        ):
            result = mcp_web_search._search("current status", 5)
        request = opened.call_args.args[0]
        self.assertIn("q=current+status", request.full_url)
        self.assertIn("[1] Current result", result)
        self.assertIn("https://news.example.test/item", result)
        self.assertNotIn("file:///etc/passwd", result)
        self.assertIn("engine-x", result)

    def test_tools_list_exposes_only_web_search(self):
        output = io.StringIO()
        with patch.object(mcp_web_search.sys, "stdout", output):
            mcp_web_search._handle({
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/list",
            })
        response = json.loads(output.getvalue())
        self.assertEqual(response["id"], 7)
        self.assertEqual(
            [tool["name"] for tool in response["result"]["tools"]],
            ["web_search"],
        )

    def test_search_rejects_out_of_contract_result_limit_before_network(self):
        with patch.object(
            mcp_web_search.urllib.request,
            "urlopen",
        ) as opened:
            with self.assertRaisesRegex(ValueError, "invalid_max_results"):
                mcp_web_search._search("current status", 0)
            with self.assertRaisesRegex(ValueError, "invalid_max_results"):
                mcp_web_search._search("current status", 11)
        opened.assert_not_called()

    def test_search_rejects_malformed_endpoint_before_network(self):
        with (
            patch.dict(
                os.environ,
                {"CHATDS_SEARXNG_SEARCH_URL": "http://search.internal:bad/search"},
                clear=False,
            ),
            patch.object(
                mcp_web_search.urllib.request,
                "urlopen",
            ) as opened,
        ):
            with self.assertRaisesRegex(RuntimeError, "search_endpoint_invalid"):
                mcp_web_search._search("current status")
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
