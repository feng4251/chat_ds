"""Typed, fixed-upstream public quote gateway.

The caller supplies only a market, symbol, and optional exchange.  It cannot
choose a URL, headers, HTTP method, or query keys.  This keeps model-authored
data out of network coordinates while still allowing isolated agent Turns to
obtain a current public quote through a small auditable capability broker.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8090
MAX_UPSTREAM_BYTES = 32 * 1024
UPSTREAM_TIMEOUT_SECONDS = 8.0
MAX_SYMBOL_CHARS = 12
_CN_SYMBOL = re.compile(r"^[0-9]{6}$")
_HK_SYMBOL = re.compile(r"^[0-9]{1,5}$")
_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_MARKETS = frozenset({"CN", "HK", "US"})
_EXCHANGES = frozenset({"AUTO", "SZ", "SH", "BJ"})


class QuoteInputError(ValueError):
    pass


class QuoteUpstreamError(RuntimeError):
    pass


def _finite_number(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _normalized_request(
    market: object,
    symbol: object,
    exchange: object = "AUTO",
) -> tuple[str, str, str, str, str]:
    if not isinstance(market, str) or not isinstance(symbol, str):
        raise QuoteInputError("invalid_market_or_symbol")
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    normalized_exchange = (
        exchange.strip().upper() if isinstance(exchange, str) else ""
    )
    if (
        normalized_market not in _MARKETS
        or not normalized_symbol
        or len(normalized_symbol) > MAX_SYMBOL_CHARS
    ):
        raise QuoteInputError("invalid_market_or_symbol")

    if normalized_market == "CN":
        if not _CN_SYMBOL.fullmatch(normalized_symbol):
            raise QuoteInputError("invalid_cn_symbol")
        if normalized_exchange not in _EXCHANGES:
            raise QuoteInputError("invalid_cn_exchange")
        resolved_exchange = normalized_exchange
        if resolved_exchange == "AUTO":
            if normalized_symbol[0] in {"0", "1", "2", "3"}:
                resolved_exchange = "SZ"
            elif normalized_symbol[0] in {"4", "8"}:
                resolved_exchange = "BJ"
            else:
                resolved_exchange = "SH"
        provider_key = resolved_exchange.lower() + normalized_symbol
        sina_key = provider_key
    elif normalized_market == "HK":
        if not _HK_SYMBOL.fullmatch(normalized_symbol):
            raise QuoteInputError("invalid_hk_symbol")
        normalized_symbol = normalized_symbol.zfill(5)
        resolved_exchange = "HK"
        provider_key = "hk" + normalized_symbol
        sina_key = provider_key
    else:
        if not _US_SYMBOL.fullmatch(normalized_symbol):
            raise QuoteInputError("invalid_us_symbol")
        resolved_exchange = "US"
        provider_key = "us" + normalized_symbol
        sina_key = "gb_" + normalized_symbol.lower()
    return (
        normalized_market,
        normalized_symbol,
        resolved_exchange,
        provider_key,
        sina_key,
    )


def _read_url(url: str, *, referer: str | None = None) -> bytes:
    headers = {
        "Accept": "text/plain,application/json;q=0.9,*/*;q=0.1",
        "User-Agent": "ChatDS-MarketData/1",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(
        request,
        timeout=UPSTREAM_TIMEOUT_SECONDS,
    ) as response:
        if int(getattr(response, "status", 200)) != 200:
            raise QuoteUpstreamError("upstream_http_status")
        payload = response.read(MAX_UPSTREAM_BYTES + 1)
    if not payload or len(payload) > MAX_UPSTREAM_BYTES:
        raise QuoteUpstreamError("upstream_payload_invalid")
    return payload


def _quoted_payload(payload: bytes, encoding: str = "gb18030") -> list[str]:
    try:
        text = payload.decode(encoding, errors="strict").strip()
    except UnicodeError as exc:
        raise QuoteUpstreamError("upstream_encoding_invalid") from exc
    first = text.find('"')
    last = text.rfind('"')
    if first < 0 or last <= first:
        raise QuoteUpstreamError("upstream_format_invalid")
    fields = text[first + 1:last].split("~" if "~" in text[first:last] else ",")
    if not fields or not any(field.strip() for field in fields):
        raise QuoteUpstreamError("upstream_quote_empty")
    return fields


def _tencent_quote(provider_key: str) -> dict[str, Any]:
    url = "https://qt.gtimg.cn/q=" + urllib.parse.quote(
        provider_key,
        safe="",
    )
    fields = _quoted_payload(_read_url(url))
    if len(fields) < 35:
        raise QuoteUpstreamError("tencent_quote_incomplete")
    last = _finite_number(fields[3])
    if last is None or last <= 0:
        raise QuoteUpstreamError("tencent_price_invalid")
    return {
        "source": "tencent_public_quote",
        "name": fields[1].strip(),
        "provider_symbol": fields[2].strip(),
        "last": last,
        "previous_close": _finite_number(fields[4]),
        "open": _finite_number(fields[5]),
        "high": _finite_number(fields[33]),
        "low": _finite_number(fields[34]),
        "change": _finite_number(fields[31]),
        "change_percent": _finite_number(fields[32]),
        "volume": _finite_number(fields[6]),
        "as_of": fields[30].strip(),
    }


def _sina_quote(market: str, sina_key: str) -> dict[str, Any]:
    url = "https://hq.sinajs.cn/list=" + urllib.parse.quote(
        sina_key,
        safe="",
    )
    fields = _quoted_payload(
        _read_url(url, referer="https://finance.sina.com.cn")
    )
    if market == "CN":
        if len(fields) < 32:
            raise QuoteUpstreamError("sina_quote_incomplete")
        quote = {
            "name": fields[0].strip(),
            "last": _finite_number(fields[3]),
            "previous_close": _finite_number(fields[2]),
            "open": _finite_number(fields[1]),
            "high": _finite_number(fields[4]),
            "low": _finite_number(fields[5]),
            "volume": _finite_number(fields[8]),
            "as_of": f"{fields[30].strip()} {fields[31].strip()}",
        }
    elif market == "HK":
        if len(fields) < 19:
            raise QuoteUpstreamError("sina_quote_incomplete")
        quote = {
            "name": fields[1].strip() or fields[0].strip(),
            "last": _finite_number(fields[6]),
            "previous_close": _finite_number(fields[3]),
            "open": _finite_number(fields[2]),
            "high": _finite_number(fields[4]),
            "low": _finite_number(fields[5]),
            "as_of": f"{fields[17].strip()} {fields[18].strip()}",
        }
    else:
        if len(fields) < 4:
            raise QuoteUpstreamError("sina_quote_incomplete")
        quote = {
            "name": fields[0].strip(),
            "last": _finite_number(fields[1]),
            "change_percent": _finite_number(fields[2]),
            "as_of": fields[3].strip(),
        }
    if quote["last"] is None or quote["last"] <= 0:
        raise QuoteUpstreamError("sina_price_invalid")
    return {"source": "sina_public_quote", **quote}


def fetch_quote(
    market: object,
    symbol: object,
    exchange: object = "AUTO",
) -> dict[str, Any]:
    (
        normalized_market,
        normalized_symbol,
        resolved_exchange,
        provider_key,
        sina_key,
    ) = _normalized_request(market, symbol, exchange)
    calls = {
        "tencent_public_quote": lambda: _tencent_quote(provider_key),
        "sina_public_quote": lambda: _sina_quote(
            normalized_market,
            sina_key,
        ),
    }
    quotes: list[dict[str, Any]] = []
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(call): name for name, call in calls.items()}
        for future, name in futures.items():
            try:
                quotes.append(future.result())
            except Exception:
                failures.append(name)
    if not quotes:
        raise QuoteUpstreamError("all_quote_sources_failed")
    quotes.sort(key=lambda row: 0 if row["source"].startswith("tencent") else 1)
    primary = quotes[0]
    corroboration = [
        {
            "source": row["source"],
            "last": row["last"],
            "as_of": row.get("as_of"),
        }
        for row in quotes[1:]
    ]
    return {
        "status": "ok",
        "market": normalized_market,
        "symbol": normalized_symbol,
        "exchange": resolved_exchange,
        "currency": {
            "CN": "CNY",
            "HK": "HKD",
            "US": "USD",
        }[normalized_market],
        "quote": primary,
        "corroboration": corroboration,
        "source_failures": sorted(failures),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "freshness": "public_near_realtime_endpoint; exchange delay may apply",
    }


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_quote_query(target: str) -> tuple[object, object, object]:
    """Parse only the bounded public quote endpoint request shape."""

    try:
        parsed = urllib.parse.urlsplit(target)
        query = urllib.parse.parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        )
    except ValueError as exc:
        raise QuoteInputError("invalid_request") from exc
    if parsed.path != "/v1/quote" or parsed.fragment:
        raise QuoteInputError("invalid_request")
    if set(query) - {"market", "symbol", "exchange"} or any(
        len(values) != 1 for values in query.values()
    ):
        raise QuoteInputError("invalid_request")
    return (
        query.get("market", [None])[0],
        query.get("symbol", [None])[0],
        query.get("exchange", ["AUTO"])[0],
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "ChatDSMarketData/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, value: dict[str, Any]) -> None:
        payload = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._send(400, {"status": "error", "error": "invalid_request"})
            return
        if parsed.path == "/health" and not parsed.query:
            self._send(200, {"status": "ok"})
            return
        if parsed.path != "/v1/quote":
            self._send(404, {"status": "error", "error": "not_found"})
            return
        try:
            market, symbol, exchange = _parse_quote_query(self.path)
        except QuoteInputError:
            self._send(400, {"status": "error", "error": "invalid_request"})
            return
        try:
            result = fetch_quote(market, symbol, exchange)
        except QuoteInputError as exc:
            self._send(400, {"status": "error", "error": str(exc)})
            return
        except Exception:
            self._send(
                502,
                {"status": "error", "error": "quote_upstream_unavailable"},
            )
            return
        self._send(200, result)


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--healthcheck":
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{LISTEN_PORT}/health",
                timeout=2,
            ) as response:
                return 0 if response.status == 200 else 1
        except Exception:
            return 1
    if len(sys.argv) != 1:
        return 2
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
