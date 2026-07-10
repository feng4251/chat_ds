import asyncio
import json
from datetime import date


_BACKENDS = ("api", "lite", "html")


def _ddg_search_sync(query: str, max_results: int = 5, backend: str | None = None) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore
    dated_query = f"{date.today().strftime('%Y年%m月%d日')} {query}"
    kwargs = {"max_results": max_results, "region": "wt-wt"}
    if backend:
        kwargs["backend"] = backend
    with DDGS() as d:
        return list(d.text(dated_query, **kwargs))


async def web_search(query: str, max_results: int = 5, timeout: float = 15.0) -> str:
    """Search DuckDuckGo and return formatted results."""
    max_results = max(1, min(int(max_results or 5), 10))
    timeout = max(5.0, min(float(timeout or 15.0), 30.0))
    attempts: list[str] = []
    results: list[dict] = []
    per_attempt_timeout = max(5.0, min(timeout, 10.0))

    for backend in _BACKENDS:
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(_ddg_search_sync, query, max_results, backend),
                timeout=per_attempt_timeout,
            )
            if results:
                break
            attempts.append(f"{backend}: no results")
        except asyncio.TimeoutError:
            attempts.append(f"{backend}: timeout")
        except Exception as e:
            attempts.append(f"{backend}: {type(e).__name__}: {str(e)[:160]}")

    if not results:
        status = "timeout" if attempts and all("timeout" in item for item in attempts) else "error"
        return json.dumps({
            "status": status,
            "error": "Web search failed after retrying DuckDuckGo backends.",
            "attempts": attempts,
        }, ensure_ascii=False)

    out = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        body = (r.get("body") or r.get("description") or "").strip()
        href = r.get("href") or r.get("url") or ""
        if len(body) > 600:
            body = body[:600].rstrip() + "…"
        out.append(f"[{i}] {title}\n{body}\nURL: {href}")
    return "\n\n".join(out)
