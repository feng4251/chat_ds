import json
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


def _fetch_error(
    url: str,
    error: str,
    *,
    failure_kind: str,
    http_status: int | None = None,
    browser_fallback_recommended: bool = False,
) -> str:
    return json.dumps({
        "status": "error",
        "url": url,
        "http_status": http_status,
        "failure_kind": failure_kind,
        "browser_fallback_recommended": browser_fallback_recommended,
        "error": f"Failed to fetch {url}: {error}",
    }, ensure_ascii=False)


def _http_failure_kind(status_code: int) -> tuple[str, bool]:
    if status_code in {401, 403}:
        return "access_denied", False
    if status_code == 429:
        return "rate_limited", False
    if status_code == 202:
        return "dynamic_page_pending", True
    return "http_status", False


def _looks_like_dynamic_shell(html: str, text: str) -> bool:
    if text.strip():
        return False
    lowered = html.casefold()
    return bool(
        re.search(r"<script\b", lowered)
        and re.search(
            r"(?:id=[\"'](?:app|root|__next)[\"']|<noscript\b|javascript)",
            lowered,
        )
    )


async def web_extract(url: str, max_chars: int = 2500, timeout: float = 10.0) -> str:
    """Fetch and extract readable text from a web page."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        ) as c:
            r = await c.get(url)
        if r.status_code != 200:
            failure_kind, browser_fallback = _http_failure_kind(r.status_code)
            return _fetch_error(
                url,
                f"HTTP {r.status_code}",
                failure_kind=failure_kind,
                http_status=r.status_code,
                browser_fallback_recommended=browser_fallback,
            )
        if not r.text:
            return _fetch_error(
                url,
                "HTTP 200 returned an empty body",
                failure_kind="empty_response",
                http_status=200,
            )
        html = r.text
    except Exception as e:
        return _fetch_error(
            url,
            f"{type(e).__name__}: {e}",
            failure_kind="transport_error",
        )

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
    if not text:
        dynamic_shell = _looks_like_dynamic_shell(html, text)
        return _fetch_error(
            url,
            "HTTP 200 contained no readable text",
            failure_kind=("dynamic_page_shell" if dynamic_shell else "empty_readable_text"),
            http_status=200,
            browser_fallback_recommended=dynamic_shell,
        )

    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text