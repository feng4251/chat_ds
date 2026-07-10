import asyncio
import json
from datetime import date
from urllib.parse import urljoin

import httpx

from config import settings


_BACKENDS = ("api", "lite", "html")


def _dated_query(query: str) -> str:
    return f"{date.today().strftime('%Y年%m月%d日')} {query}"


def _ddg_search_sync(query: str, max_results: int = 5, backend: str | None = None) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore
    kwargs = {"max_results": max_results, "region": "wt-wt"}
    if backend:
        kwargs["backend"] = backend
    with DDGS() as d:
        return list(d.text(_dated_query(query), **kwargs))


async def _search_searxng(query: str, max_results: int, timeout: float) -> list[dict]:
    base_url = str(settings.searxng_base_url).rstrip("/") + "/"
    url = urljoin(base_url, "search")
    params = {
        "q": query,
        "format": "json",
        "safesearch": "1",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    results = []
    for item in payload.get("results") or []:
        title = (item.get("title") or "").strip()
        href = item.get("url") or ""
        body = (item.get("content") or item.get("description") or "").strip()
        if title and href:
            results.append({"title": title, "href": href, "body": body})
        if len(results) >= max_results:
            break
    return results


async def _search_ddg(query: str, max_results: int, per_attempt_timeout: float, attempts: list[str]) -> list[dict]:
    for backend in _BACKENDS:
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(_ddg_search_sync, query, max_results, backend),
                timeout=per_attempt_timeout,
            )
            if results:
                return results
            attempts.append(f"ddg/{backend}: no results")
        except asyncio.TimeoutError:
            attempts.append(f"ddg/{backend}: timeout")
        except Exception as e:
            attempts.append(f"ddg/{backend}: {type(e).__name__}: {str(e)[:160]}")
    return []


def _format_results(results: list[dict]) -> str:
    out = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        body = (r.get("body") or r.get("description") or "").strip()
        href = r.get("href") or r.get("url") or ""
        if len(body) > 600:
            body = body[:600].rstrip() + "…"
        out.append(f"[{i}] {title}\n{body}\nURL: {href}")
    return "\n\n".join(out)


async def web_search(query: str, max_results: int = 5, timeout: float = 15.0) -> str:
    """Search configured providers and return formatted results."""
    max_results = max(1, min(int(max_results or 5), 10))
    timeout = max(5.0, min(float(timeout or 15.0), 30.0))
    attempts: list[str] = []
    per_attempt_timeout = max(5.0, min(timeout, 10.0))
    providers = [p.strip().lower() for p in settings.web_search_providers.split(",") if p.strip()]

    for provider in providers:
        if provider == "searxng":
            try:
                results = await _search_searxng(
                    query,
                    max_results,
                    max(3.0, min(float(settings.searxng_timeout_seconds or 10.0), timeout)),
                )
                if results:
                    return _format_results(results)
                attempts.append("searxng: no results")
            except asyncio.TimeoutError:
                attempts.append("searxng: timeout")
            except httpx.TimeoutException:
                attempts.append("searxng: timeout")
            except httpx.HTTPStatusError as e:
                attempts.append(f"searxng: HTTP {e.response.status_code}")
            except Exception as e:
                attempts.append(f"searxng: {type(e).__name__}: {str(e)[:160]}")
        elif provider in {"ddg", "duckduckgo", "ddgs"}:
            results = await _search_ddg(query, max_results, per_attempt_timeout, attempts)
            if results:
                return _format_results(results)
        else:
            attempts.append(f"{provider}: unsupported provider")

    if not providers:
        attempts.append("providers: none configured")
    status = "timeout" if attempts and all("timeout" in item for item in attempts) else "error"
    return json.dumps({
        "status": status,
        "error": "Web search failed after trying configured providers.",
        "attempts": attempts,
    }, ensure_ascii=False)
