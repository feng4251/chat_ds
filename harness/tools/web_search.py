import asyncio
import json
from datetime import date


def _ddg_search_sync(query: str, max_results: int = 5) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore
    dated_query = f"{date.today().strftime('%Y年%m月%d日')} {query}"
    with DDGS() as d:
        return list(d.text(dated_query, max_results=max_results, region="wt-wt"))


async def web_search(query: str, max_results: int = 5, timeout: float = 15.0) -> str:
    """Search DuckDuckGo and return formatted results."""
    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(_ddg_search_sync, query, max_results),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return '{"status":"timeout","error":"Web search timed out."}'
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"Web search failed: {type(e).__name__}: {e}",
        }, ensure_ascii=False)

    if not results:
        return "(No search results found.)"

    out = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        body = (r.get("body") or r.get("description") or "").strip()
        href = r.get("href") or r.get("url") or ""
        if len(body) > 600:
            body = body[:600].rstrip() + "…"
        out.append(f"[{i}] {title}\n{body}\nURL: {href}")
    return "\n\n".join(out)