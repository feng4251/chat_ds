import unittest
from unittest.mock import patch

from market_data_gateway import server


class _Response:
    status = 200

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int):
        return self.payload[:limit]


class MarketDataGatewayTests(unittest.TestCase):
    def test_cn_quote_uses_fixed_provider_coordinates(self):
        tencent_fields = [""] * 35
        for index, value in {
            0: "51", 1: "德明利", 2: "001309", 3: "410.84",
            4: "400.80", 5: "397.50", 6: "97907",
            30: "20260811104739", 31: "10.04", 32: "2.50",
            33: "411.68", 34: "395.02",
        }.items():
            tencent_fields[index] = value
        tencent = (
            'v_sz001309="' + "~".join(tencent_fields) + '";'
        ).encode("gb18030")
        sina_fields = ["德明利", "397.50", "400.80", "410.90", "411.68", "395.02"]
        sina_fields.extend(["0"] * 24)
        sina_fields.extend(["2026-08-11", "10:47:42", "00"])
        sina = ('var hq_str_sz001309="' + ",".join(sina_fields) + '";').encode("gb18030")

        def fake_open(request, timeout):
            self.assertEqual(timeout, server.UPSTREAM_TIMEOUT_SECONDS)
            if request.full_url == "https://qt.gtimg.cn/q=sz001309":
                return _Response(tencent)
            if request.full_url == "https://hq.sinajs.cn/list=sz001309":
                self.assertEqual(
                    request.headers["Referer"],
                    "https://finance.sina.com.cn",
                )
                return _Response(sina)
            raise AssertionError(request.full_url)

        with patch.object(server.urllib.request, "urlopen", side_effect=fake_open):
            result = server.fetch_quote("cn", "001309")
        self.assertEqual(result["quote"]["last"], 410.84)
        self.assertEqual(result["corroboration"][0]["last"], 410.9)
        self.assertEqual(result["exchange"], "SZ")
        self.assertEqual(result["currency"], "CNY")

    def test_input_never_accepts_a_url_or_query_fragment_as_symbol(self):
        with patch.object(server.urllib.request, "urlopen") as opened:
            for value in (
                "https://attacker.invalid/x",
                "001309&leak=secret",
                "../001309",
            ):
                with self.assertRaises(server.QuoteInputError):
                    server.fetch_quote("CN", value)
        opened.assert_not_called()

    def test_request_parser_rejects_extra_duplicate_and_malformed_fields(self):
        for target in (
            "/v1/quote?market=CN&symbol=001309&callback=leak",
            "/v1/quote?market=CN&symbol=001309&symbol=600000",
            "/v1/quote?market=CN&symbol",
            "/v1/quote?market=CN&symbol=001309&exchange=AUTO&x=1&y=2",
        ):
            with self.subTest(target=target):
                with self.assertRaises(server.QuoteInputError):
                    server._parse_quote_query(target)

    def test_us_and_hk_keys_are_canonical(self):
        self.assertEqual(
            server._normalized_request("US", "nvda")[3:],
            ("usNVDA", "gb_nvda"),
        )
        self.assertEqual(
            server._normalized_request("HK", "700")[3:],
            ("hk00700", "hk00700"),
        )


if __name__ == "__main__":
    unittest.main()
