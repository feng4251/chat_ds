import re

import httpx

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)


def _strip_html(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<noscript[^>]*>.*?</noscript>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


async def web_extract(url: str, max_chars: int = 2500, timeout: float = 10.0) -> str:
    """Fetch and extract readable text from a web page."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        ) as c:
            r = await c.get(url)
        if r.status_code != 200 or not r.text:
            return f"(Failed to fetch {url}: HTTP {r.status_code})"
        html = r.text
    except Exception as e:
        return f"(Failed to fetch {url}: {e})"

    text = ""
    try:
        import trafilatura
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        ) or ""
    except Exception:
        text = ""

    if not text:
        text = _strip_html(html)

    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text