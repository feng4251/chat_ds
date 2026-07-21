"""Fault-tolerant web metasearch broker.

SearXNG is the primary aggregation boundary.  This module deliberately treats
an upstream response as data, not as proof of success: challenge pages,
off-domain ``site:`` results, malformed URLs, and wholly unrelated result sets
must not prevent another configured provider from being tried.

The cache, singleflight table, semaphore, and circuit breaker are process-local
on purpose.  The production harness currently runs one uvicorn worker.  They
reduce duplicate fan-out from parallel agents without introducing a new
service dependency; SearXNG remains responsible for its own per-engine
suspension policy.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import threading
import time
import unicodedata
import weakref
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date
from html import unescape
from typing import Any, Iterable, Sequence
from urllib.parse import (
    parse_qsl,
    urlencode,
    unquote,
    urljoin,
    urlsplit,
    urlunsplit,
)

import httpx

from config import settings


# ``ddgs>=9`` no longer implements the historical api/lite/html names.  An
# unknown name silently falls back to its ``auto`` metasearch, so iterating the
# old tuple would issue the same broad request three times.  Keep the final
# fallback explicitly on DuckDuckGo and issue it once.
_BACKENDS = ("duckduckgo",)
_MAX_RESULTS = 10
_MAX_UPSTREAM_RESULTS = 40
_MAX_CACHE_ENTRIES = 512
_CACHE_TTL_SECONDS = 3600.0
_FRESH_CACHE_TTL_SECONDS = 300.0
_STALE_TTL_SECONDS = 21_600.0
_UPSTREAM_CONCURRENCY = 4
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SECONDS = 120.0

_CJK_RUN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]+"
)
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", re.I)
_SITE_OPERATOR_RE = re.compile(
    r"(?<![-\w])site\s*:\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s()]+))",
    re.I,
)
_OTHER_OPERATOR_RE = re.compile(
    r"(?<![-\w])(?:filetype|ext|inurl|intitle|allinurl|allintitle|"
    r"before|after|source)\s*:\s*(?:\"[^\"]*\"|'[^']*'|[^\s()]+)",
    re.I,
)
_HTML_TAG_RE = re.compile(r"<[^>]{1,500}>")
_WHITESPACE_RE = re.compile(r"\s+")
_TEMPORAL_RE = re.compile(
    r"\b(?:latest|today|current|currently|breaking|recent|recently|"
    r"now|this\s+(?:week|month|year)|news|live|updated?)\b",
    re.I,
)
_TEMPORAL_CJK_MARKERS = (
    "最新",
    "今天",
    "今日",
    "当前",
    "实时",
    "近期",
    "本周",
    "本月",
    "今年",
    "新闻",
    "刚刚",
)
_CHALLENGE_HOST_MARKERS = (
    "challenges.cloudflare.com",
    "captcha.baidu.com",
    "wappass.baidu.com",
    "recaptcha.net",
    "google.com/recaptcha",
)
_CHALLENGE_MARKERS = (
    "verify you are human",
    "verification required",
    "unusual traffic",
    "one more step",
    "complete the security check",
    "enable javascript and cookies to continue",
    "sorry, you have been blocked",
    "automated queries",
    "cf-chl-",
    "challenge-platform",
    "cf-turnstile",
    "access denied",
    "请求过于频繁",
    "访问被拒绝",
    "请输入验证码",
    "安全验证",
)
_TRACKING_QUERY_KEYS = frozenset({
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
    "spm",
    "yclid",
})
_LATIN_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "what", "when", "where", "which", "who", "why", "with",
    "official", "website", "web", "search", "find", "about",
})


@dataclass(frozen=True)
class SearchResult:
    """Provider-neutral result while retaining metasearch provenance."""

    title: str
    href: str
    body: str = ""
    engines: tuple[str, ...] = ()
    score: float = 0.0
    provider: str = ""
    published_date: str = ""


@dataclass(frozen=True)
class SearchBatch:
    results: tuple[SearchResult, ...] = ()
    unresponsive_engines: tuple[tuple[str, str], ...] = ()
    raw_count: int = 0


@dataclass(frozen=True)
class _FilterReport:
    accepted: tuple[SearchResult, ...]
    rejected: tuple[tuple[str, int], ...]

    def count(self, reason: str) -> int:
        return dict(self.rejected).get(reason, 0)


@dataclass(frozen=True)
class _SearchSnapshot:
    results: tuple[SearchResult, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SearchOutcome:
    snapshot: _SearchSnapshot | None
    attempts: tuple[str, ...]
    complete: bool
    timed_out: bool = False


@dataclass
class _CacheEntry:
    snapshot: _SearchSnapshot
    fresh_until: float
    stale_until: float
    complete: bool
    degraded: bool


@dataclass
class _CircuitState:
    failures: int = 0
    opened_until: float = 0.0
    probe_inflight: bool = False
    last_error: str = ""


@dataclass(frozen=True)
class _CircuitPermit:
    key: str
    probe: bool = False


class _ProviderFailure(RuntimeError):
    def __init__(self, message: str, *, timed_out: bool = False):
        super().__init__(message)
        self.timed_out = timed_out


_STATE_LOCK = threading.RLock()
_CACHE: OrderedDict[str, _CacheEntry] = OrderedDict()
_CIRCUITS: dict[str, _CircuitState] = {}
_FLIGHTS: dict[tuple[int, str], asyncio.Task[_SearchOutcome]] = {}
_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _monotonic() -> float:
    return time.monotonic()


def _bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    """Clamp an internal broker bound.

    These controls are intentionally code-local in this change.  Pretending an
    undeclared pydantic Settings field is environment-configurable is worse
    than an explicit safe default; deployment-level tuning can add typed
    Settings/compose fields as one atomic follow-up.
    """

    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(maximum, value))


def _cache_capacity() -> int:
    return int(_bounded_number(_MAX_CACHE_ENTRIES, 512, 1, 10_000))


def _cache_ttls(query: str) -> tuple[float, float]:
    default_fresh = (
        _FRESH_CACHE_TTL_SECONDS if _is_time_sensitive_query(query)
        else _CACHE_TTL_SECONDS
    )
    fresh = _bounded_number(default_fresh, _CACHE_TTL_SECONDS, 1.0, 86_400.0)
    if _is_time_sensitive_query(query):
        fresh = min(fresh, _FRESH_CACHE_TTL_SECONDS)
    stale = _bounded_number(_STALE_TTL_SECONDS, 21_600.0, fresh, 604_800.0)
    return fresh, stale


def _upstream_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _STATE_LOCK:
        semaphore = _SEMAPHORES.get(loop)
        if semaphore is None:
            limit = int(_bounded_number(_UPSTREAM_CONCURRENCY, 4, 1, 32))
            semaphore = asyncio.Semaphore(limit)
            _SEMAPHORES[loop] = semaphore
        return semaphore


def _normalize_space(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def _clean_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = unescape(_HTML_TAG_RE.sub(" ", value.replace("\x00", " ")))
    return _normalize_space(text)[:limit]


def _normalized_query(query: str) -> str:
    return _normalize_space(query).casefold()


def _is_time_sensitive_query(query: str) -> bool:
    normalized = unicodedata.normalize("NFKC", query)
    return bool(_TEMPORAL_RE.search(normalized)) or any(
        marker in normalized for marker in _TEMPORAL_CJK_MARKERS
    )


def _dated_query(query: str) -> str:
    """Add a date only when the caller explicitly asks for fresh information."""

    query = _normalize_space(query)
    if not _is_time_sensitive_query(query):
        return query
    return f"{date.today().isoformat()} {query}"


def _simplified_query(query: str) -> str | None:
    words = [
        word.strip(' ,;:()[]{}')
        for word in query.split()
        if word.strip(' ,;:()[]{}')
    ]
    if len(words) < 8:
        return None
    simplified = " ".join(words[:8])
    return simplified if simplified and simplified != query else None


def _failure_hints(query: str) -> list[str]:
    simplified = _simplified_query(query)
    hints = [
        "Retry with a shorter query focused on 3-8 distinctive terms.",
        "Try an English synonym if the original query is Chinese or mixed-language.",
        "For evidence-heavy work, search an official source/domain plus the entity, then extract the result page.",
        "If diagnostics report a circuit-open provider, wait for its cooldown instead of retrying it repeatedly.",
    ]
    if simplified:
        hints.insert(0, f"Suggested shorter query: {simplified}")
    return hints


def _normalize_site_domain(raw: str) -> str | None:
    value = unicodedata.normalize("NFKC", raw).strip().strip(".,;:()[]{}")
    value = value.removeprefix("*.").rstrip(".")
    if "://" in value:
        value = urlsplit(value).hostname or ""
    else:
        value = value.split("/", 1)[0]
        if value.count(":") == 1:
            host, possible_port = value.rsplit(":", 1)
            if possible_port.isdigit():
                value = host
    value = value.casefold().rstrip(".")
    if not value or any(char.isspace() for char in value):
        return None
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if "." not in value and value != "localhost":
        return None
    return value


def _site_domains(query: str) -> tuple[str, ...]:
    domains: list[str] = []
    normalized = unicodedata.normalize("NFKC", query)
    for match in _SITE_OPERATOR_RE.finditer(normalized):
        raw = next((group for group in match.groups() if group is not None), "")
        domain = _normalize_site_domain(raw)
        if domain and domain not in domains:
            domains.append(domain)
    return tuple(domains)


def _query_without_operators(query: str) -> str:
    text = unicodedata.normalize("NFKC", query)
    text = _SITE_OPERATOR_RE.sub(" ", text)
    text = _OTHER_OPERATOR_RE.sub(" ", text)
    text = re.sub(r"\b(?:AND|OR|NOT)\b", " ", text, flags=re.I)
    return _normalize_space(text.replace('"', " ").replace("'", " "))


def _query_signals(query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    text = _query_without_operators(query).casefold()
    latin: list[str] = []
    for token in _LATIN_TOKEN_RE.findall(text):
        collapsed = re.sub(r"[-_.]", "", token)
        candidates = (token, collapsed) if collapsed != token else (token,)
        for candidate in candidates:
            if (
                len(candidate) >= 2
                and candidate not in _LATIN_STOPWORDS
                and candidate not in latin
            ):
                latin.append(candidate)

    cjk: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        if len(run) <= 4:
            grams = (run,)
        else:
            grams = tuple(run[index:index + 2] for index in range(len(run) - 1))
        for gram in grams:
            if gram and gram not in cjk:
                cjk.append(gram)
    return tuple(latin), tuple(cjk)


def _latin_signal_present(corpus: str, token: str) -> bool:
    """Match one Latin query signal without accepting substring accidents."""

    return bool(re.search(
        rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])",
        corpus,
        re.IGNORECASE,
    ))


def _mixed_query_anchor_matches(
    query: str,
    corpus: str,
    *,
    engines: Sequence[str] = (),
) -> bool:
    """Require the most distinctive Latin concept in a mixed-script query.

    A max-of-language relevance score is useful for cross-language recall, but
    by itself a result about only the Chinese half of ``Galectin-3 阿尔茨海默病``
    looks perfect.  Keep that recall while requiring the highest-specificity
    Latin concept (identifier/digit-bearing terms first).  An exact anchor in
    an otherwise-Latin result is accepted only when independent meta-search
    engines agree, or when the user supplied an explicit site restriction.
    That preserves cross-language recall without allowing one polluted engine
    to pass a result merely because it mentions a common scientific term.
    A result containing both language concepts remains sufficient on its own;
    the much weaker numeric-suffix fallback also requires a CJK match.
    """

    text = _query_without_operators(query).casefold()
    raw_tokens = [
        token for token in _LATIN_TOKEN_RE.findall(text)
        if len(token) >= 2 and token not in _LATIN_STOPWORDS
    ]
    if not raw_tokens or not _CJK_RUN_RE.search(text):
        return True

    def specificity(token: str) -> tuple[int, int, int]:
        return (
            int(any(char.isdigit() for char in token)),
            int(any(char in "-_." for char in token)),
            min(len(token), 64),
        )

    anchor = max(raw_tokens, key=specificity)
    variants = tuple(dict.fromkeys((anchor, re.sub(r"[-_.]", "", anchor))))
    _, cjk = _query_signals(query)
    has_cjk_match = any(gram in corpus for gram in cjk)
    has_exact_anchor = any(
        variant and _latin_signal_present(corpus, variant)
        for variant in variants
    )
    if has_exact_anchor:
        distinct_engines = {
            str(engine).strip().casefold()
            for engine in engines
            if str(engine).strip()
        }
        return bool(
            has_cjk_match
            or len(distinct_engines) >= 2
            or _site_domains(query)
        )

    if not has_cjk_match:
        return False

    # Scientific identifiers are often translated while retaining a suffix
    # such as ``-3``.  Require that exact punctuated suffix; a bare digit is far
    # too weak because it commonly appears in unrelated result URLs.
    numeric_suffixes = tuple(dict.fromkeys(re.findall(r"[-_.]\d+", anchor)))
    return bool(numeric_suffixes) and all(
        suffix in corpus for suffix in numeric_suffixes
    )


def _validated_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > 4096 or any(ord(char) < 32 or char.isspace() for char in url):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host.encode("idna")
        except UnicodeError:
            return None
    else:
        if not address.is_global:
            return None
    if port is not None and not (1 <= port <= 65535):
        return None
    return url


def _canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    port = parsed.port
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    retained_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_KEYS:
            continue
        retained_query.append((key, value))
    query = urlencode(sorted(retained_query))
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, query, ""))


def _url_matches_sites(url: str, sites: Sequence[str]) -> bool:
    if not sites:
        return True
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return any(host == site or host.endswith("." + site) for site in sites)


def _is_challenge_result(result: SearchResult) -> bool:
    haystack = " ".join((result.title, result.body, result.href)).casefold()
    if any(marker in haystack for marker in _CHALLENGE_HOST_MARKERS):
        return True
    return any(marker in haystack for marker in _CHALLENGE_MARKERS)


def _relevance_score(query: str, result: SearchResult) -> float:
    latin, cjk = _query_signals(query)
    if not latin and not cjk:
        return 1.0

    title = unicodedata.normalize("NFKC", unquote(result.title)).casefold()
    body = unicodedata.normalize("NFKC", unquote(result.body)).casefold()
    url_text = unicodedata.normalize("NFKC", unquote(result.href)).casefold()
    corpus = " ".join((title, body, url_text))

    if latin and cjk and not _mixed_query_anchor_matches(
        query,
        corpus,
        engines=result.engines,
    ):
        return 0.0

    latin_score = 0.0
    if latin:
        weights = {token: max(1.0, min(len(token), 12) / 4.0) for token in latin}
        total = sum(weights.values())
        matched = sum(
            weight for token, weight in weights.items()
            if _latin_signal_present(corpus, token)
        )
        latin_score = matched / total if total else 0.0
        if any(_latin_signal_present(title, token) for token in latin):
            latin_score += 0.25
        elif any(_latin_signal_present(url_text, token) for token in latin):
            latin_score += 0.12

    cjk_score = 0.0
    if cjk:
        matched = sum(1 for gram in cjk if gram in corpus)
        cjk_score = matched / len(cjk)
        if any(gram in title for gram in cjk):
            cjk_score += 0.20

    return min(1.0, max(latin_score, cjk_score))


def _filter_results(query: str, results: Iterable[SearchResult]) -> _FilterReport:
    sites = _site_domains(query)
    accepted: list[SearchResult] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for result in results:
        href = _validated_url(result.href)
        if href is None:
            reject("invalid_url")
            continue
        candidate = replace(result, href=href)
        if _is_challenge_result(candidate):
            reject("challenge_or_access_denied")
            continue
        if not _url_matches_sites(candidate.href, sites):
            reject("site_mismatch")
            continue
        if _relevance_score(query, candidate) < 0.18:
            reject("irrelevant")
            continue
        accepted.append(candidate)
    return _FilterReport(tuple(accepted), tuple(sorted(rejected.items())))


def _normalize_engines(item: dict[str, Any], provider: str) -> tuple[str, ...]:
    engines: list[str] = []
    raw = item.get("engines")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple, set)):
        for value in raw:
            if isinstance(value, str) and value.strip():
                name = value.strip().casefold()
                if name not in engines:
                    engines.append(name)
    singular = item.get("engine")
    if isinstance(singular, str) and singular.strip():
        name = singular.strip().casefold()
        if name not in engines:
            engines.append(name)
    if not engines and provider:
        engines.append(provider)
    return tuple(engines)


def _normalize_result(item: Any, provider: str) -> SearchResult | None:
    if not isinstance(item, dict):
        return None
    title = _clean_text(item.get("title"), limit=1000)
    href = item.get("url") or item.get("href") or ""
    body = _clean_text(
        item.get("content") or item.get("body") or item.get("description"),
        limit=4000,
    )
    if not title or not isinstance(href, str) or not href.strip():
        return None
    try:
        score = float(item.get("score") or 0.0)
    except (TypeError, ValueError, OverflowError):
        score = 0.0
    if not math.isfinite(score):
        score = 0.0
    published = item.get("publishedDate") or item.get("published_date") or ""
    if published and not isinstance(published, str):
        published = str(published)
    return SearchResult(
        title=title,
        href=href.strip(),
        body=body,
        engines=_normalize_engines(item, provider),
        score=score,
        provider=provider,
        published_date=str(published)[:100],
    )


def _normalize_unresponsive(payload: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    for item in payload.get("unresponsive_engines") or []:
        engine = ""
        reason = "unresponsive"
        if isinstance(item, (list, tuple)) and item:
            engine = str(item[0]).strip()
            if len(item) > 1 and item[1]:
                reason = str(item[1]).strip()
        elif isinstance(item, dict):
            engine = str(item.get("engine") or item.get("name") or "").strip()
            reason = str(item.get("error") or item.get("reason") or reason).strip()
        elif isinstance(item, str):
            engine = item.strip()
        if engine:
            pair = (engine.casefold(), reason[:200])
            if pair not in normalized:
                normalized.append(pair)
    return tuple(normalized)


def _ddg_search_sync(
    query: str,
    max_results: int = 5,
    backend: str | None = None,
) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore
    kwargs: dict[str, Any] = {"max_results": max_results, "region": "wt-wt"}
    if backend:
        kwargs["backend"] = backend
    with DDGS() as client:
        return list(client.text(_dated_query(query), **kwargs))


async def _search_searxng(query: str, max_results: int, timeout: float) -> SearchBatch:
    base_url = str(settings.searxng_base_url).rstrip("/") + "/"
    url = urljoin(base_url, "search")
    params = {"q": query, "format": "json", "safesearch": "1"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("SearXNG returned a non-object JSON payload")
    raw_results = payload.get("results") or []
    if not isinstance(raw_results, list):
        raise ValueError("SearXNG results is not a list")
    limit = min(_MAX_UPSTREAM_RESULTS, max(_MAX_RESULTS * 2, max_results * 3))
    normalized = [
        result
        for result in (
            _normalize_result(item, "searxng") for item in raw_results[:limit]
        )
        if result is not None
    ]
    return SearchBatch(
        results=tuple(normalized),
        unresponsive_engines=_normalize_unresponsive(payload),
        raw_count=len(raw_results),
    )


async def _search_ddg(
    query: str,
    max_results: int,
    per_attempt_timeout: float,
    attempts: list[str],
) -> SearchBatch:
    errors = 0
    clean_empty = False
    rejected_results: list[SearchResult] = []
    rejected_raw_count = 0
    fetch_count = min(_MAX_UPSTREAM_RESULTS, max(max_results * 3, _MAX_RESULTS))
    for backend in _BACKENDS:
        try:
            raw_results = await asyncio.wait_for(
                asyncio.to_thread(_ddg_search_sync, query, fetch_count, backend),
                timeout=per_attempt_timeout,
            )
            normalized = tuple(
                result
                for result in (
                    _normalize_result(item, "duckduckgo") for item in raw_results
                )
                if result is not None
            )
            if normalized:
                report = _filter_results(query, normalized)
                if report.accepted:
                    # Preserve the full batch so the common outer quality gate
                    # can retain rejection diagnostics and provenance.
                    return SearchBatch(results=normalized, raw_count=len(raw_results))
                rejected_results.extend(normalized)
                rejected_raw_count += len(raw_results)
                summary = ", ".join(
                    f"{reason}={count}" for reason, count in report.rejected
                )
                attempts.append(f"ddg/{backend}: no accepted results ({summary})")
                clean_empty = True
                continue
            clean_empty = True
            attempts.append(f"ddg/{backend}: no results")
        except asyncio.TimeoutError:
            errors += 1
            attempts.append(f"ddg/{backend}: timeout")
        except Exception as exc:  # provider packages expose several exception types
            errors += 1
            attempts.append(
                f"ddg/{backend}: {type(exc).__name__}: {str(exc)[:160]}"
            )
    if errors and not clean_empty:
        raise _ProviderFailure(
            "all DDG backends failed",
            timed_out=errors == len(_BACKENDS)
            and all("timeout" in item for item in attempts[-len(_BACKENDS):]),
        )
    return SearchBatch(
        results=tuple(rejected_results),
        raw_count=rejected_raw_count,
    )


def _merge_results(
    existing: Sequence[SearchResult],
    incoming: Iterable[SearchResult],
) -> tuple[SearchResult, ...]:
    merged = list(existing)
    positions = {_canonical_url(result.href): index for index, result in enumerate(merged)}
    for result in incoming:
        key = _canonical_url(result.href)
        position = positions.get(key)
        if position is None:
            positions[key] = len(merged)
            merged.append(result)
            continue
        previous = merged[position]
        engines = tuple(dict.fromkeys(previous.engines + result.engines))
        title = previous.title if len(previous.title) >= len(result.title) else result.title
        body = previous.body if len(previous.body) >= len(result.body) else result.body
        merged[position] = SearchResult(
            title=title,
            href=previous.href,
            body=body,
            engines=engines,
            score=max(previous.score, result.score),
            provider=previous.provider or result.provider,
            published_date=previous.published_date or result.published_date,
        )
    return tuple(merged)


def _endpoint_key(provider: str) -> str:
    if provider == "searxng":
        return f"searxng:{str(settings.searxng_base_url).rstrip('/').casefold()}"
    return provider


def _circuit_begin(key: str) -> tuple[_CircuitPermit | None, str | None]:
    now = _monotonic()
    with _STATE_LOCK:
        state = _CIRCUITS.setdefault(key, _CircuitState())
        if state.opened_until > now:
            remaining = max(1, math.ceil(state.opened_until - now))
            return None, (
                f"circuit open; retry after {remaining}s"
                + (f" ({state.last_error})" if state.last_error else "")
            )
        if state.opened_until:
            if state.probe_inflight:
                return None, "circuit half-open; recovery probe already in flight"
            state.probe_inflight = True
            return _CircuitPermit(key=key, probe=True), None
        return _CircuitPermit(key=key), None


def _circuit_success(permit: _CircuitPermit) -> None:
    with _STATE_LOCK:
        state = _CIRCUITS.setdefault(permit.key, _CircuitState())
        state.failures = 0
        state.opened_until = 0.0
        state.probe_inflight = False
        state.last_error = ""


def _circuit_failure(permit: _CircuitPermit, error: str) -> None:
    threshold = int(_bounded_number(_CIRCUIT_FAILURE_THRESHOLD, 3, 1, 20))
    cooldown = _bounded_number(
        _CIRCUIT_COOLDOWN_SECONDS, 120.0, 1.0, 86_400.0
    )
    with _STATE_LOCK:
        state = _CIRCUITS.setdefault(permit.key, _CircuitState())
        state.failures += 1
        state.probe_inflight = False
        state.last_error = error[:200]
        if permit.probe or state.failures >= threshold:
            state.opened_until = _monotonic() + cooldown


async def _fetch_provider(
    provider: str,
    query: str,
    timeout: float,
    attempts: list[str],
) -> tuple[SearchBatch, _CircuitPermit] | None:
    key = _endpoint_key(provider)
    permit: _CircuitPermit | None = None
    try:
        # Queue locally before reserving a circuit permit.  A broker deadline
        # can cancel a crowded caller while it is waiting for this semaphore;
        # that is local load shedding, not evidence that the upstream endpoint
        # failed.  In particular it must not consume a half-open probe or open
        # an otherwise healthy circuit.
        async with _upstream_semaphore():
            permit, blocked = _circuit_begin(key)
            if permit is None:
                attempts.append(f"{provider}: {blocked}")
                return None
            if provider == "searxng":
                searx_timeout = max(
                    3.0,
                    min(
                        float(settings.searxng_timeout_seconds or 10.0),
                        timeout,
                    ),
                )
                batch = await _search_searxng(query, _MAX_RESULTS, searx_timeout)
            else:
                batch = await _search_ddg(
                    query,
                    _MAX_RESULTS,
                    max(5.0, min(timeout, 10.0)),
                    attempts,
                )
        return batch, permit
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        if permit is not None:
            _circuit_failure(permit, "timeout")
        attempts.append(f"{provider}: timeout")
        raise _ProviderFailure(str(exc) or "timeout", timed_out=True) from exc
    except httpx.HTTPStatusError as exc:
        reason = f"HTTP {exc.response.status_code}"
        if permit is not None:
            _circuit_failure(permit, reason)
        attempts.append(f"{provider}: {reason}")
        raise _ProviderFailure(reason) from exc
    except _ProviderFailure as exc:
        if permit is not None:
            _circuit_failure(permit, str(exc))
        attempts.append(f"{provider}: {str(exc)[:160]}")
        raise
    except asyncio.CancelledError:
        # The broker's absolute live-search deadline may cancel an in-flight
        # provider. Release and count a real in-flight permit, but do not blame
        # the endpoint when cancellation happened only in the local queue.
        if permit is not None:
            _circuit_failure(permit, "cancelled by broker deadline")
        else:
            attempts.append(
                f"{provider}: local concurrency queue deadline before dispatch"
            )
        raise
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:160]}"
        if permit is not None:
            _circuit_failure(permit, reason)
        attempts.append(f"{provider}: {reason}")
        raise _ProviderFailure(reason) from exc


def _provider_names() -> tuple[str, ...]:
    names: list[str] = []
    for raw in str(settings.web_search_providers).split(","):
        provider = raw.strip().casefold()
        if provider in {"duckduckgo", "ddgs"}:
            provider = "ddg"
        if provider and provider not in names:
            names.append(provider)
    return tuple(names)


def _cache_key(query: str, providers: Sequence[str]) -> str:
    endpoint = str(settings.searxng_base_url).rstrip("/").casefold()
    return json.dumps(
        [_normalized_query(query), list(providers), endpoint],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _cache_lookup(
    key: str,
    required_results: int,
) -> tuple[_CacheEntry | None, _CacheEntry | None]:
    now = _monotonic()
    with _STATE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None, None
        if entry.stale_until <= now:
            _CACHE.pop(key, None)
            return None, None
        _CACHE.move_to_end(key)
        adequate = len(entry.snapshot.results) >= required_results or entry.complete
        if entry.fresh_until > now and adequate:
            return entry, None
        return None, entry


def _cache_store(
    key: str,
    query: str,
    snapshot: _SearchSnapshot,
    complete: bool,
    *,
    degraded: bool,
) -> None:
    if not snapshot.results:
        return
    fresh_ttl, stale_ttl = _cache_ttls(query)
    # Coverage and health are separate axes.  A healthy target-satisfied early
    # stop is intentionally ``complete=False`` so a future larger max_results
    # can expand it, but the results it did return deserve the normal TTL.
    # Only actual provider degradation receives the short recovery TTL.
    if degraded:
        fresh_ttl = min(fresh_ttl, 60.0)
    now = _monotonic()
    with _STATE_LOCK:
        existing = _CACHE.get(key)
        # Differently-sized concurrent flights deliberately do not share a
        # producer.  If the richer flight finishes first, the later small
        # flight must not downgrade its fresh cache entry.  Do not merge an
        # expired/stale entry into a genuinely new refresh.
        if existing is not None and existing.fresh_until > now:
            incoming_count = len(snapshot.results)
            existing_count = len(existing.snapshot.results)
            # A later weaker flight must not downgrade a richer healthy cache;
            # conversely, a healthy expansion clears an older degraded state.
            if (
                (not existing.degraded and existing_count >= incoming_count)
                or (not degraded and incoming_count >= existing_count)
            ):
                degraded = False
            else:
                degraded = existing.degraded or degraded
            snapshot = _SearchSnapshot(
                results=_merge_results(
                    existing.snapshot.results,
                    snapshot.results,
                ),
                notes=tuple(dict.fromkeys(
                    existing.snapshot.notes + snapshot.notes
                )),
            )
            complete = existing.complete or complete
            if degraded:
                fresh_ttl = min(fresh_ttl, 60.0)
        _CACHE[key] = _CacheEntry(
            snapshot=snapshot,
            fresh_until=(
                now + fresh_ttl
                if degraded
                else max(
                    now + fresh_ttl,
                    existing.fresh_until if existing is not None else 0.0,
                )
            ),
            stale_until=max(
                now + stale_ttl,
                existing.stale_until if existing is not None else 0.0,
            ),
            complete=complete,
            degraded=degraded,
        )
        _CACHE.move_to_end(key)
        while len(_CACHE) > _cache_capacity():
            _CACHE.popitem(last=False)


def _unresponsive_note(entries: Sequence[tuple[str, str]]) -> str | None:
    if not entries:
        return None
    rendered = ", ".join(f"{engine} ({reason})" for engine, reason in entries)
    return f"SearXNG unresponsive engines: {rendered}"


async def _perform_search(
    query: str,
    target_results: int,
    timeout: float,
    providers: tuple[str, ...],
    cache_key: str,
    stale_entry: _CacheEntry | None,
) -> _SearchOutcome:
    attempts: list[str] = []
    notes: list[str] = []
    merged: tuple[SearchResult, ...] = ()
    timed_out = False
    degraded = False
    budget_exhausted = False
    deadline = asyncio.get_running_loop().time() + timeout
    queries = [query]
    simplified = _simplified_query(query)
    if simplified:
        queries.append(simplified)
    complete = True

    if not providers:
        attempts.append("providers: none configured")

    stop = False
    for query_variant in queries:
        if query_variant != query:
            attempts.append(f"retry: simplified query '{query_variant}'")
        for provider in providers:
            if provider not in {"searxng", "ddg"}:
                attempts.append(f"{provider}: unsupported provider")
                degraded = True
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                attempts.append(
                    f"broker: total live-search timeout exhausted after {timeout:.1f}s"
                )
                timed_out = True
                degraded = True
                budget_exhausted = True
                break
            try:
                fetched = await asyncio.wait_for(
                    _fetch_provider(
                        provider,
                        query_variant,
                        min(timeout, remaining),
                        attempts,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                attempts.append(
                    f"broker: total live-search timeout exhausted after {timeout:.1f}s"
                )
                timed_out = True
                degraded = True
                budget_exhausted = True
                break
            except _ProviderFailure as exc:
                timed_out = timed_out or exc.timed_out
                degraded = True
                continue
            if fetched is None:
                degraded = True
                continue
            batch, permit = fetched
            note = _unresponsive_note(batch.unresponsive_engines)
            if note and note not in notes:
                notes.append(note)
                degraded = True
            report = _filter_results(query, batch.results)
            rejected = dict(report.rejected)
            if report.accepted:
                _circuit_success(permit)
                merged = _merge_results(merged, report.accepted)
                if rejected.get("challenge_or_access_denied", 0):
                    degraded = True
            elif batch.raw_count == 0:
                _circuit_success(permit)
                attempts.append(f"{provider}: no results")
            elif rejected.get("challenge_or_access_denied", 0) == batch.raw_count:
                # The endpoint returned valid HTTP/JSON.  Result semantics are
                # not endpoint health: SearXNG owns per-engine CAPTCHA
                # suspension, while this broker's endpoint circuit is reserved
                # for transport, HTTP, and protocol failures.
                _circuit_success(permit)
                degraded = True
                attempts.append(
                    f"{provider}: rejected {batch.raw_count} challenge/access-denied results"
                )
            elif rejected.get("irrelevant", 0) == batch.raw_count and batch.raw_count >= 3:
                degraded = True
                _circuit_success(permit)
                attempts.append(
                    f"{provider}: rejected {batch.raw_count} irrelevant/polluted "
                    "results without opening the endpoint circuit"
                )
            else:
                _circuit_success(permit)
                summary = ", ".join(
                    f"{reason}={count}" for reason, count in report.rejected
                )
                attempts.append(f"{provider}: no accepted results ({summary})")

            if len(merged) >= target_results:
                complete = False
                stop = True
                break
        if stop:
            break
        if budget_exhausted:
            break

    if degraded:
        complete = False

    if merged:
        snapshot = _SearchSnapshot(results=merged, notes=tuple(notes))
        _cache_store(
            cache_key,
            query,
            snapshot,
            complete,
            degraded=degraded,
        )
        return _SearchOutcome(
            snapshot=snapshot,
            attempts=tuple(attempts),
            complete=complete,
            timed_out=timed_out,
        )

    if stale_entry is not None:
        stale_note = "Serving stale cached results because every live provider was unavailable or returned no acceptable results."
        stale_snapshot = _SearchSnapshot(
            results=stale_entry.snapshot.results,
            notes=tuple(dict.fromkeys(stale_entry.snapshot.notes + (stale_note,))),
        )
        return _SearchOutcome(
            snapshot=stale_snapshot,
            attempts=tuple(attempts),
            complete=stale_entry.complete,
            timed_out=timed_out,
        )

    return _SearchOutcome(
        snapshot=None,
        attempts=tuple(attempts),
        complete=True,
        timed_out=timed_out,
    )


def _flight_done(
    flight_key: tuple[int, str],
    task: asyncio.Task[_SearchOutcome],
) -> None:
    with _STATE_LOCK:
        if _FLIGHTS.get(flight_key) is task:
            _FLIGHTS.pop(flight_key, None)


def _shared_search_task(
    flight_key: tuple[int, str],
    factory: Any,
) -> asyncio.Task[_SearchOutcome]:
    loop = asyncio.get_running_loop()
    with _STATE_LOCK:
        task = _FLIGHTS.get(flight_key)
        if task is None or task.done():
            task = loop.create_task(factory())
            _FLIGHTS[flight_key] = task
            task.add_done_callback(lambda completed: _flight_done(flight_key, completed))
        return task


def _format_results(
    results: Sequence[SearchResult | dict[str, Any]],
    notes: Sequence[str] = (),
) -> str:
    out: list[str] = []
    for index, raw in enumerate(results, 1):
        if isinstance(raw, SearchResult):
            result = raw
        else:
            result = _normalize_result(raw, "")
            if result is None:
                continue
        body = result.body
        if len(body) > 600:
            body = body[:600].rstrip() + "…"
        lines = [f"[{index}] {result.title}"]
        if body:
            lines.append(body)
        lines.append(f"URL: {result.href}")
        if result.engines:
            lines.append(f"Sources: {', '.join(result.engines)}")
        out.append("\n".join(lines))
    if notes:
        out.append("Search diagnostics:\n" + "\n".join(f"- {note}" for note in notes))
    return "\n\n".join(out)


def _failure_payload(query: str, outcome: _SearchOutcome) -> str:
    attempts = list(outcome.attempts)
    status = "timeout" if outcome.timed_out and attempts else "error"
    return json.dumps(
        {
            "status": status,
            "error": "Web search failed after trying configured providers; no quality-accepted result was available.",
            "attempts": attempts,
            "hints": _failure_hints(query),
        },
        ensure_ascii=False,
    )


async def web_search(query: str, max_results: int = 5, timeout: float = 15.0) -> str:
    """Return quality-checked results within one total live-search time budget.

    ``timeout`` bounds the whole cache-miss/provider chain, not every provider
    independently.  Cache hits do not consume that live-search budget.
    """

    query = _normalize_space(str(query or ""))
    if not query:
        return json.dumps(
            {
                "status": "error",
                "error": "Web search query must not be empty.",
                "attempts": [],
                "hints": ["Provide at least one distinctive search term."],
            },
            ensure_ascii=False,
        )
    max_results = max(1, min(int(max_results or 5), _MAX_RESULTS))
    timeout = max(5.0, min(float(timeout or 15.0), 30.0))
    providers = _provider_names()
    key = _cache_key(query, providers)
    fresh_entry, stale_entry = _cache_lookup(key, max_results)
    if fresh_entry is not None:
        return _format_results(
            fresh_entry.snapshot.results[:max_results],
            fresh_entry.snapshot.notes,
        )

    loop = asyncio.get_running_loop()
    # Differing result/latency requirements must not share an undersized or
    # prematurely-timed-out producer.  Equal calls still collapse to one
    # upstream flight, while their cache entries remain reusable across sizes.
    flight_key = (
        id(loop),
        f"{key}|max={max_results}|timeout={timeout:.3f}",
    )
    task = _shared_search_task(
        flight_key,
        lambda: _perform_search(
            query,
            max_results,
            timeout,
            providers,
            key,
            stale_entry,
        ),
    )
    try:
        outcome = await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # final containment: tool failures are data, not run aborts
        outcome = _SearchOutcome(
            snapshot=None,
            attempts=(f"broker: {type(exc).__name__}: {str(exc)[:160]}",),
            complete=True,
        )
    if outcome.snapshot is None:
        return _failure_payload(query, outcome)
    return _format_results(
        outcome.snapshot.results[:max_results],
        outcome.snapshot.notes,
    )


def _reset_search_state_for_tests() -> None:
    """Clear process-local broker state.  Tests must call this between loops."""

    with _STATE_LOCK:
        for task in tuple(_FLIGHTS.values()):
            if not task.done():
                task.cancel()
        _FLIGHTS.clear()
        _CACHE.clear()
        _CIRCUITS.clear()
        _SEMAPHORES.clear()
