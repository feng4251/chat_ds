"""Small requests-compatible facade for bundled MCP scripts.

The harness already ships httpx. This covers the common ``requests.get/post``
surface without allowing uploaded skills to trigger runtime package installs.
"""

from __future__ import annotations

import httpx

RequestException = httpx.HTTPError
HTTPError = httpx.HTTPStatusError
Timeout = httpx.TimeoutException
ConnectionError = httpx.ConnectError
Response = httpx.Response


def request(method: str, url: str, **kwargs) -> httpx.Response:
    return httpx.request(method, url, **kwargs)


def get(url: str, **kwargs) -> httpx.Response:
    return httpx.get(url, **kwargs)


def post(url: str, **kwargs) -> httpx.Response:
    return httpx.post(url, **kwargs)


def put(url: str, **kwargs) -> httpx.Response:
    return httpx.put(url, **kwargs)


def patch(url: str, **kwargs) -> httpx.Response:
    return httpx.patch(url, **kwargs)


def delete(url: str, **kwargs) -> httpx.Response:
    return httpx.delete(url, **kwargs)
