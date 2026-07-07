"""Skill registry + dispatcher.

A skill is a pre-processor that augments a user's message before it is sent
to the LLM. run_skill_stream is the canonical interface — it's an async
generator that yields {"type": "progress", "msg": str} events while running,
and a final {"type": "result", "result": SkillResult} event.

Add new skills by appending an entry to SKILLS and a branch in run_skill_stream.
"""
import asyncio
import json
import re
from datetime import date
from typing import Optional, AsyncIterator

import httpx
from pydantic import BaseModel

from config import settings


class SkillResult(BaseModel):
    augmented_content: str
    augmented_image_urls: Optional[list[str]] = None
    system_note: Optional[str] = None


SKILLS = [
    {
        "id": "general",
        "name": "普通对话",
        "description": "直接与所选模型对话",
    },
    {
        "id": "web_search",
        "name": "联网搜索",
        "description": "回答前先用 DuckDuckGo 检索",
    },
    {
        "id": "research",
        "name": "深度研究",
        "description": "规划子查询 → 抓正文 → 综合并附引用",
    },
]


# ---------- DuckDuckGo search ----------

def _ddg_search_sync(query: str, max_results: int):
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore
    dated_query = f"{date.today().strftime('%Y年%m月%d日')} {query}"
    with DDGS() as d:
        return list(d.text(dated_query, max_results=max_results, region="wt-wt"))


async def _ddg_search(query: str, max_results: int = 5, timeout: float = 15.0):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_ddg_search_sync, query, max_results),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"error": "search timed out"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _format_search_results(results, body_limit: int = 600) -> str:
    if isinstance(results, dict) and "error" in results:
        return f"(Web search failed: {results['error']})"
    if not results:
        return "(No search results found.)"
    out = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        body = (r.get("body") or r.get("description") or "").strip()
        href = r.get("href") or r.get("url") or ""
        if len(body) > body_limit:
            body = body[:body_limit].rstrip() + "…"
        out.append(f"[{i}] {title}\n{body}\nURL: {href}")
    return "\n\n".join(out)


# ---------- Page fetch + content extraction ----------

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


async def _fetch_and_extract(url: str, max_chars: int = 2500, timeout: float = 10.0) -> str:
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        ) as c:
            r = await c.get(url)
        if r.status_code != 200 or not r.text:
            return ""
        html = r.text
    except Exception:
        return ""

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


# ---------- Sub-query planning (LLM call) ----------

async def _plan_subqueries(query: str, max_n: int = 3, timeout: float = 30.0) -> list[str]:
    """Use qwen3_6 (thinking disabled) to decompose the question into sub-queries."""
    prompt = (
        f"Decompose this research question into up to {max_n} focused, diverse "
        "web-search queries that together would cover the topic. Return ONLY the "
        "queries, one per line, no numbering, no commentary.\n\n"
        f"Research question: {query}"
    )
    content = ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream(
                "POST",
                f"{settings.qwen3_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.qwen3_api_key}"},
                json={
                    "model": "qwen3_6",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0.3,
                    "stream": True,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            ) as r:
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:]
                    if chunk == "[DONE]":
                        break
                    try:
                        delta = json.loads(chunk)["choices"][0].get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            content += piece
                    except Exception:
                        continue
    except Exception:
        return [query]

    out: list[str] = []
    for ln in content.split("\n"):
        ln = re.sub(r"^[-*•\d.\)\(\s]+", "", ln).strip().strip('"').strip("'")
        if 4 < len(ln) < 200:
            out.append(ln)
    return out[:max_n] or [query]


# ---------- Dispatcher ----------

async def run_skill_stream(
    skill_id: Optional[str],
    content: str,
    image_urls: Optional[list[str]],
) -> AsyncIterator[dict]:
    """Async generator yielding progress events and a final result event."""
    if not skill_id or skill_id == "general":
        yield {"type": "result", "result": SkillResult(
            augmented_content=content,
            augmented_image_urls=image_urls,
        )}
        return

    if skill_id == "web_search":
        q = (content or "").strip()
        yield {"type": "progress", "msg": f"🔎 正在搜索:{q[:80]}"}
        results = await _ddg_search(content, max_results=5)
        if isinstance(results, dict) and "error" in results:
            yield {"type": "progress", "msg": f"⚠️ 搜索失败:{results['error']}"}
        else:
            yield {"type": "progress", "msg": f"✓ 找到 {len(results)} 条结果"}
        ctx = _format_search_results(results)
        augmented = (
            "Below are recent web search results for the user's question.\n\n"
            f"{ctx}\n\n"
            "---\n"
            f"User question: {content}\n\n"
            "Answer the question using the search results above when relevant. "
            "Cite sources inline by their bracket number (e.g. [1])."
        )
        note = (
            "Web Search skill is active. The user message includes fresh web "
            "results — cite by their bracket number (e.g. [1]). If the results "
            "say search failed, say so transparently and answer from your own "
            "knowledge."
        )
        yield {"type": "result", "result": SkillResult(
            augmented_content=augmented,
            augmented_image_urls=image_urls,
            system_note=note,
        )}
        return

    if skill_id == "research":
        yield {"type": "progress", "msg": "🧭 规划子查询…"}
        subqueries = await _plan_subqueries(content)
        yield {"type": "progress", "msg": f"✓ 生成 {len(subqueries)} 个子查询:"}
        for q in subqueries:
            yield {"type": "progress", "msg": f"   · {q}"}

        yield {"type": "progress", "msg": "🔎 并行检索每条子查询…"}
        search_outcomes = await asyncio.gather(
            *(_ddg_search(q, max_results=3) for q in subqueries),
            return_exceptions=True,
        )

        seen: set[str] = set()
        candidates: list[dict] = []
        for outcome in search_outcomes:
            if isinstance(outcome, Exception) or isinstance(outcome, dict):
                continue
            for r in outcome:
                href = r.get("href") or r.get("url") or ""
                if not href or href in seen:
                    continue
                seen.add(href)
                candidates.append({
                    "url": href,
                    "title": (r.get("title") or "").strip(),
                    "snippet": (r.get("body") or r.get("description") or "").strip(),
                })
                if len(candidates) >= 6:
                    break
            if len(candidates) >= 6:
                break

        yield {"type": "progress", "msg": f"✓ 收集到 {len(candidates)} 条候选链接"}

        if not candidates:
            sources: list[dict] = []
        else:
            yield {"type": "progress", "msg": f"📄 抓取 {len(candidates)} 篇正文…"}
            fetched = await asyncio.gather(
                *(_fetch_and_extract(c["url"], max_chars=2500) for c in candidates),
                return_exceptions=True,
            )
            success = 0
            for c, text in zip(candidates, fetched):
                ok = isinstance(text, str) and text and text != c["snippet"]
                if ok:
                    success += 1
                c["text"] = text if isinstance(text, str) and text else c["snippet"]
            yield {"type": "progress", "msg": f"✓ 抽到正文 {success}/{len(candidates)} 篇"}
            sources = candidates

        yield {"type": "progress", "msg": "📝 综合研究档案…"}

        sub_str = "\n".join(f"- {q}" for q in subqueries) or "- (none)"
        if sources:
            src_str = "\n\n".join(
                f"[{i}] {s['title']}\nURL: {s['url']}\n\n{s['text']}"
                for i, s in enumerate(sources, 1)
            )
        else:
            src_str = "(No sources could be fetched. Search may have failed.)"

        augmented = (
            "Deep Research dossier for the user's question.\n\n"
            f"Sub-queries searched:\n{sub_str}\n\n"
            f"Sources retrieved:\n\n{src_str}\n\n"
            "---\n"
            f"User question: {content}\n\n"
            "Write a thorough research-style answer with these sections:\n"
            "1. **Background** — relevant context.\n"
            "2. **Key findings** — what the sources say, with [n] citations.\n"
            "3. **Open questions / gaps** — where sources disagree or are silent.\n"
            "4. **Sources** — the numbered list from above with their URLs.\n\n"
            "Cite [n] inline. Be precise about what comes from the sources vs. "
            "your own knowledge. If the dossier is empty, say so and answer "
            "from your knowledge with the caveat."
        )
        note = (
            "Deep Research skill is active. The user message contains a dossier "
            "of sub-queries, fetched sources, and snippets. Produce a structured, "
            "citation-heavy report following the requested section layout."
        )
        yield {"type": "result", "result": SkillResult(
            augmented_content=augmented,
            augmented_image_urls=image_urls,
            system_note=note,
        )}
        return

    # Unknown skill id — passthrough
    yield {"type": "result", "result": SkillResult(
        augmented_content=content,
        augmented_image_urls=image_urls,
    )}


async def run_skill(
    skill_id: Optional[str],
    content: str,
    image_urls: Optional[list[str]],
) -> SkillResult:
    """Non-streaming wrapper — collects all events and returns the final result."""
    result: Optional[SkillResult] = None
    async for evt in run_skill_stream(skill_id, content, image_urls):
        if evt.get("type") == "result":
            result = evt["result"]
    return result or SkillResult(augmented_content=content, augmented_image_urls=image_urls)
