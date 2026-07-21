import asyncio
import importlib
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx

search = importlib.import_module("tools.web_search")


def result(
    title: str,
    href: str,
    body: str = "",
    *,
    engines=("baidu",),
    score: float = 1.0,
    provider: str = "searxng",
) -> search.SearchResult:
    return search.SearchResult(
        title=title,
        href=href,
        body=body,
        engines=tuple(engines),
        score=score,
        provider=provider,
    )


def batch(*items: search.SearchResult, raw_count=None, unresponsive=()):
    return search.SearchBatch(
        results=tuple(items),
        raw_count=len(items) if raw_count is None else raw_count,
        unresponsive_engines=tuple(unresponsive),
    )


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("GET", "http://searx.test/search")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "fixture failure", request=self.request, response=self
            )

    def json(self):
        return self._payload


class FakeClient:
    payload = {}
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params):
        type(self).calls.append((url, params))
        return FakeResponse(type(self).payload)


class SearchNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        search._reset_search_state_for_tests()
        FakeClient.calls = []

    async def asyncTearDown(self):
        search._reset_search_state_for_tests()

    async def test_searx_payload_preserves_all_general_engine_provenance(self):
        engines = ("baidu", "360search", "sogou", "yahoo", "mojeek")
        FakeClient.payload = {
            "results": [
                {
                    "title": f"OpenAI result from {engine}",
                    "url": f"https://{engine}.example.com/openai",
                    "content": "OpenAI research and product information",
                    "engine": engine,
                    "engines": [engine],
                    "positions": [1],
                    "score": index + 0.5,
                    "publishedDate": "2026-07-21T00:00:00",
                }
                for index, engine in enumerate(engines)
            ],
            "unresponsive_engines": [
                ["bing", "Suspended: CAPTCHA"],
                ["sogou", "timeout"],
            ],
        }
        with (
            patch.object(search.httpx, "AsyncClient", FakeClient),
            patch.object(search.settings, "searxng_base_url", "http://searx.test"),
        ):
            actual = await search._search_searxng("OpenAI", 5, 7)

        self.assertEqual(engines, tuple(item.engines[0] for item in actual.results))
        self.assertEqual(0.5, actual.results[0].score)
        self.assertEqual("2026-07-21T00:00:00", actual.results[0].published_date)
        self.assertEqual(
            (("bing", "Suspended: CAPTCHA"), ("sogou", "timeout")),
            actual.unresponsive_engines,
        )
        self.assertEqual("OpenAI", FakeClient.calls[0][1]["q"])

    async def test_searx_payload_must_be_an_object_with_result_list(self):
        with patch.object(search.httpx, "AsyncClient", FakeClient):
            FakeClient.payload = []
            with self.assertRaisesRegex(ValueError, "non-object"):
                await search._search_searxng("test", 5, 5)
            FakeClient.payload = {"results": {"title": "wrong shape"}}
            with self.assertRaisesRegex(ValueError, "not a list"):
                await search._search_searxng("test", 5, 5)

    async def test_result_normalization_accepts_description_and_merged_engines(self):
        actual = search._normalize_result(
            {
                "title": "  OpenAI &amp; SearXNG ",
                "href": "https://example.com/result",
                "description": "<b>OpenAI</b> evidence",
                "engine": "baidu",
                "engines": ["baidu", "yahoo", "baidu"],
                "score": "2.5",
            },
            "searxng",
        )
        self.assertIsNotNone(actual)
        self.assertEqual("OpenAI & SearXNG", actual.title)
        self.assertEqual("OpenAI evidence", actual.body)
        self.assertEqual(("baidu", "yahoo"), actual.engines)
        self.assertEqual(2.5, actual.score)


class SearchQualityTests(unittest.TestCase):
    def test_site_filter_accepts_exact_and_subdomain_only(self):
        query = "site:nih.gov Galectin-3 Alzheimer"
        report = search._filter_results(
            query,
            (
                result(
                    "Galectin-3 in Alzheimer disease",
                    "https://nih.gov/article",
                    "Galectin-3 Alzheimer evidence",
                ),
                result(
                    "Galectin-3 PubMed record",
                    "https://pubmed.ncbi.nlm.nih.gov/123",
                    "Alzheimer disease",
                ),
                result(
                    "Galectin-3 fake record",
                    "https://nih.gov.evil.example/123",
                    "Alzheimer disease",
                ),
                result(
                    "Galectin-3 fake record",
                    "https://notnih.gov/123",
                    "Alzheimer disease",
                ),
            ),
        )
        self.assertEqual(2, len(report.accepted))
        self.assertEqual(2, report.count("site_mismatch"))

    def test_site_filter_cannot_be_bypassed_with_credentials_or_redirect_host(self):
        report = search._filter_results(
            "site:pubmed.ncbi.nlm.nih.gov galectin-3",
            (
                result(
                    "galectin-3",
                    "https://pubmed.ncbi.nlm.nih.gov@evil.example/123",
                ),
                result(
                    "galectin-3",
                    "https://www.baidu.com/link?target=pubmed.ncbi.nlm.nih.gov",
                ),
            ),
        )
        self.assertFalse(report.accepted)
        self.assertEqual(1, report.count("invalid_url"))
        self.assertEqual(1, report.count("site_mismatch"))

    def test_mixed_language_query_requires_both_concepts(self):
        chinese = result(
            "阿尔茨海默病中的半乳糖凝集素-3",
            "https://example.com/cn",
            "靶点机制与临床研究证据",
        )
        english = result(
            "Galectin-3 as a target in Alzheimer's disease",
            "https://example.com/en",
            "Clinical development evidence for LGALS3",
            engines=("yahoo", "mojeek"),
        )
        single_engine_latin_only = result(
            "Serum Galectin-3 levels in rheumatoid arthritis",
            "https://polluted.example.com/galectin-3",
            "A single-engine result about an unrelated indication",
            engines=("mojeek",),
        )
        junk = result(
            "Docker deployment tutorial",
            "https://example.com/docker",
            "Container networking and compose examples",
            engines=("bing",),
        )
        one_concept_pollution = result(
            "阿尔茨海默病百科",
            "https://example.com/alzheimer-only",
            "阿尔茨海默病的病因、症状和治疗",
            engines=("bing",),
        )
        report = search._filter_results(
            "Galectin-3 阿尔茨海默病 临床开发",
            (
                chinese,
                english,
                single_engine_latin_only,
                junk,
                one_concept_pollution,
            ),
        )
        # The English result preserves the exact distinctive Latin anchor and
        # has independent engine consensus, so cross-language retrieval keeps
        # it.  A single engine mentioning only that anchor cannot hide its lack
        # of the other-language concept; the CJK-only one-concept result also
        # fails.
        self.assertEqual((chinese, english), report.accepted)
        self.assertEqual(3, report.count("irrelevant"))

        english_report = search._filter_results(
            "Galectin-3 Alzheimer clinical development",
            (english, junk),
        )
        self.assertEqual((english,), english_report.accepted)

        # A user-supplied site boundary is an independent precision signal, so
        # a single engine may still return a translated result from that exact
        # site without requiring a second metasearch engine to duplicate it.
        site_scoped_translation = result(
            "Galectin-3 as a target in Alzheimer's disease",
            "https://trusted.example.com/galectin-3-ad",
            "Clinical development evidence for the translated disease concept",
            engines=("yahoo",),
        )
        site_report = search._filter_results(
            "site:trusted.example.com Galectin-3 阿尔茨海默病 临床开发",
            (site_scoped_translation,),
        )
        self.assertEqual((site_scoped_translation,), site_report.accepted)

    def test_challenge_access_denied_and_illegal_urls_are_hard_rejected(self):
        report = search._filter_results(
            "OpenAI",
            (
                result(
                    "One more step",
                    "https://challenges.cloudflare.com/turnstile",
                    "Verify you are human",
                    engines=("bing",),
                ),
                result(
                    "Access denied",
                    "https://www.bing.com/challenge",
                    "Enable JavaScript and cookies to continue",
                    engines=("bing",),
                ),
                result("OpenAI", "javascript:alert(1)"),
                result("OpenAI", "http://127.0.0.1/private"),
                result("OpenAI research", "https://openai.com/research"),
            ),
        )
        self.assertEqual(1, len(report.accepted))
        self.assertEqual(2, report.count("challenge_or_access_denied"))
        self.assertEqual(2, report.count("invalid_url"))

    def test_tracking_parameters_do_not_prevent_deduplication(self):
        first = result(
            "OpenAI research",
            "https://example.com/research?utm_source=bing&id=7#section",
            engines=("baidu",),
        )
        second = result(
            "OpenAI research and publications",
            "https://example.com/research?id=7",
            "Longer OpenAI research evidence body",
            engines=("yahoo",),
        )
        merged = search._merge_results((first,), (second,))
        self.assertEqual(1, len(merged))
        self.assertEqual(("baidu", "yahoo"), merged[0].engines)
        self.assertEqual(second.title, merged[0].title)


class SearchBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        search._reset_search_state_for_tests()

    async def asyncTearDown(self):
        search._reset_search_state_for_tests()

    async def test_polluted_nonempty_searx_does_not_suppress_ddg_fallback(self):
        polluted = batch(
            result("Docker tutorial", "https://junk.example/docker", engines=("bing",)),
            result("Weather today", "https://junk.example/weather", engines=("bing",)),
            result("Football scores", "https://junk.example/sport", engines=("bing",)),
        )
        fallback = batch(
            result(
                "Galectin-3 in Alzheimer disease",
                "https://pubmed.ncbi.nlm.nih.gov/123",
                "Galectin-3 evidence in Alzheimer disease",
                engines=("duckduckgo",),
                provider="duckduckgo",
            )
        )
        with (
            patch.object(search.settings, "web_search_providers", "searxng,ddg"),
            patch.object(search, "_search_searxng", AsyncMock(return_value=polluted)),
            patch.object(search, "_search_ddg", AsyncMock(return_value=fallback)) as ddg,
        ):
            output = await search.web_search("Galectin-3 Alzheimer", max_results=1)
        self.assertIn("pubmed.ncbi.nlm.nih.gov", output)
        self.assertNotIn("Docker tutorial", output)
        ddg.assert_awaited_once()

    async def test_valid_engines_survive_while_polluted_engine_is_removed(self):
        searx = batch(
            result(
                "OpenAI official research",
                "https://openai.com/research",
                engines=("baidu",),
            ),
            result(
                "Unrelated shopping coupons",
                "https://junk.example/coupons",
                engines=("bing",),
            ),
            result(
                "OpenAI documentation",
                "https://platform.openai.com/docs",
                engines=("yahoo",),
            ),
        )
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_search_searxng", AsyncMock(return_value=searx)),
        ):
            output = await search.web_search("OpenAI documentation", max_results=5)
        self.assertIn("Sources: baidu", output)
        self.assertIn("Sources: yahoo", output)
        self.assertNotIn("shopping coupons", output)

    async def test_provider_results_merge_and_deduplicate(self):
        searx = batch(
            result(
                "OpenAI research",
                "https://example.com/openai?utm_source=baidu",
                engines=("baidu",),
            )
        )
        ddg_batch = batch(
            result(
                "OpenAI research overview",
                "https://example.com/openai",
                engines=("duckduckgo",),
                provider="duckduckgo",
            ),
            result(
                "OpenAI safety",
                "https://example.org/openai-safety",
                engines=("duckduckgo",),
                provider="duckduckgo",
            ),
        )
        with (
            patch.object(search.settings, "web_search_providers", "searxng,ddg"),
            patch.object(search, "_search_searxng", AsyncMock(return_value=searx)),
            patch.object(search, "_search_ddg", AsyncMock(return_value=ddg_batch)),
        ):
            output = await search.web_search("OpenAI", max_results=3)
        self.assertEqual(1, output.count("URL: https://example.com/openai"))
        self.assertIn("Sources: baidu, duckduckgo", output)
        self.assertIn("openai-safety", output)

    async def test_site_mismatch_falls_through_to_matching_provider(self):
        with (
            patch.object(search.settings, "web_search_providers", "searxng,ddg"),
            patch.object(
                search,
                "_search_searxng",
                AsyncMock(return_value=batch(result(
                    "Galectin-3 paper", "https://evil.example/paper"
                ))),
            ),
            patch.object(
                search,
                "_search_ddg",
                AsyncMock(return_value=batch(result(
                    "Galectin-3 PubMed paper",
                    "https://pubmed.ncbi.nlm.nih.gov/123",
                    engines=("duckduckgo",),
                    provider="duckduckgo",
                ))),
            ),
        ):
            output = await search.web_search(
                "site:pubmed.ncbi.nlm.nih.gov Galectin-3", max_results=1
            )
        self.assertIn("pubmed.ncbi.nlm.nih.gov/123", output)
        self.assertNotIn("evil.example", output)

    async def test_unresponsive_engines_are_preserved_as_success_diagnostics(self):
        searx = batch(
            result("OpenAI research", "https://openai.com/research"),
            unresponsive=(("bing", "Suspended: CAPTCHA"),),
        )
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_search_searxng", AsyncMock(return_value=searx)),
        ):
            output = await search.web_search("OpenAI", max_results=1)
        self.assertIn("SearXNG unresponsive engines", output)
        self.assertIn("bing (Suspended: CAPTCHA)", output)

    async def test_empty_unresponsive_searx_still_uses_fallback(self):
        searx = batch(unresponsive=(("bing", "timeout"),))
        fallback = batch(result(
            "OpenAI homepage",
            "https://openai.com/",
            engines=("duckduckgo",),
            provider="duckduckgo",
        ))
        with (
            patch.object(search.settings, "web_search_providers", "searxng,ddg"),
            patch.object(search, "_search_searxng", AsyncMock(return_value=searx)),
            patch.object(search, "_search_ddg", AsyncMock(return_value=fallback)) as ddg,
        ):
            output = await search.web_search("OpenAI", max_results=1)
        self.assertIn("openai.com", output)
        ddg.assert_awaited_once()

    async def test_success_is_cached_without_a_second_upstream_call(self):
        upstream = AsyncMock(return_value=batch(result(
            "OpenAI research", "https://openai.com/research"
        )))
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_search_searxng", upstream),
        ):
            first = await search.web_search("OpenAI", max_results=1)
            second = await search.web_search("  openai  ", max_results=1)
        self.assertEqual(first, second)
        self.assertEqual(1, upstream.await_count)

    async def test_healthy_early_stop_keeps_normal_ttl_but_larger_request_expands(self):
        now = [1000.0]
        upstream = AsyncMock(side_effect=[
            batch(result("OpenAI item 0", "https://example.com/0")),
            batch(*(
                result(f"OpenAI item {index}", f"https://example.com/{index}")
                for index in range(5)
            )),
        ])
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_monotonic", side_effect=lambda: now[0]),
            patch.object(search, "_search_searxng", upstream),
        ):
            first = await search.web_search("OpenAI", 1)
            entry = next(iter(search._CACHE.values()))
            self.assertFalse(entry.complete)
            self.assertFalse(entry.degraded)
            self.assertEqual(search._CACHE_TTL_SECONDS, entry.fresh_until - now[0])

            # This used to refetch at 60 seconds merely because the healthy
            # producer stopped after satisfying max_results=1.
            now[0] += 61
            same_size = await search.web_search("OpenAI", 1)
            self.assertEqual(1, upstream.await_count)

            # Coverage remains incomplete, so a larger request bypasses the
            # otherwise-fresh small entry and expands it.
            larger = await search.web_search("OpenAI", 5)

        self.assertEqual(first, same_size)
        self.assertEqual(5, larger.count("URL: "))
        self.assertEqual(2, upstream.await_count)

    async def test_ttl_expiry_refreshes_and_transient_failure_serves_stale(self):
        now = [1000.0]
        upstream = AsyncMock(side_effect=[
            batch(result("OpenAI v1", "https://openai.com/v1")),
            httpx.ReadTimeout("fixture timeout"),
        ])
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_monotonic", side_effect=lambda: now[0]),
            patch.object(search, "_CACHE_TTL_SECONDS", 10.0),
            patch.object(search, "_STALE_TTL_SECONDS", 100.0),
            patch.object(search, "_search_searxng", upstream),
        ):
            first = await search.web_search("OpenAI", max_results=1)
            now[0] += 11
            stale = await search.web_search("OpenAI", max_results=1)
        self.assertIn("OpenAI v1", first)
        self.assertIn("OpenAI v1", stale)
        self.assertIn("Serving stale cached results", stale)
        self.assertEqual(2, upstream.await_count)

    async def test_cache_is_bounded_lru(self):
        async def upstream(query, max_results, timeout):
            slug = query.casefold().replace(" ", "-")
            return batch(result(query, f"https://example.com/{slug}"))

        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_MAX_CACHE_ENTRIES", 2),
            patch.object(search, "_search_searxng", side_effect=upstream),
        ):
            for query in ("Alpha", "Beta", "Gamma"):
                await search.web_search(query, max_results=1)
        self.assertEqual(2, len(search._CACHE))

    async def test_concurrent_equivalent_queries_are_singleflight(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def upstream(query, max_results, timeout):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return batch(result("OpenAI research", "https://openai.com/research"))

        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_search_searxng", side_effect=upstream),
        ):
            tasks = [asyncio.create_task(search.web_search("OpenAI", 1)) for _ in range(20)]
            await asyncio.wait_for(started.wait(), 1)
            release.set()
            outputs = await asyncio.gather(*tasks)
        self.assertEqual(1, calls)
        self.assertEqual(1, len(set(outputs)))

    async def test_different_result_limits_do_not_share_an_undersized_flight(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def upstream(query, max_results, timeout):
            nonlocal calls
            calls += 1
            call_number = calls
            if call_number == 1:
                first_started.set()
                await release_first.wait()
                return batch(result("OpenAI item 0", "https://example.com/0"))
            return batch(*(
                result(f"OpenAI item {index}", f"https://example.com/{index}")
                for index in range(10)
            ))

        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_search_searxng", side_effect=upstream),
        ):
            small = asyncio.create_task(search.web_search("OpenAI", 1))
            await asyncio.wait_for(first_started.wait(), 1)
            large = asyncio.create_task(search.web_search("OpenAI", 10))
            large_output = await large
            release_first.set()
            await small
        self.assertEqual(2, calls)
        self.assertEqual(10, large_output.count("URL: "))

    async def test_cancelled_waiter_does_not_cancel_shared_producer(self):
        started = asyncio.Event()
        release = asyncio.Event()
        upstream = AsyncMock()

        async def delayed(query, max_results, timeout):
            started.set()
            await release.wait()
            return batch(result("OpenAI research", "https://openai.com/research"))

        upstream.side_effect = delayed
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_search_searxng", upstream),
        ):
            cancelled = asyncio.create_task(search.web_search("OpenAI", 1))
            survivor = asyncio.create_task(search.web_search("OpenAI", 1))
            await asyncio.wait_for(started.wait(), 1)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            release.set()
            output = await survivor
        self.assertIn("OpenAI research", output)
        self.assertEqual(1, upstream.await_count)

    async def test_failed_flight_is_cleaned_and_next_call_can_retry(self):
        upstream = AsyncMock(side_effect=[
            httpx.ReadTimeout("fixture timeout"),
            batch(result("OpenAI recovered", "https://openai.com/recovered")),
        ])
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_search_searxng", upstream),
        ):
            first = json.loads(await search.web_search("OpenAI first", 1))
            second = await search.web_search("OpenAI second", 1)
        self.assertEqual("timeout", first["status"])
        self.assertIn("OpenAI recovered", second)
        self.assertEqual(2, upstream.await_count)

    async def test_late_small_flight_cannot_downgrade_richer_fresh_cache(self):
        small_started = asyncio.Event()
        release_small = asyncio.Event()
        calls = 0

        async def upstream(query, max_results, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                small_started.set()
                await release_small.wait()
                return batch(result("OpenAI result 1", "https://example.com/1"))
            return batch(*(
                result(f"OpenAI result {index}", f"https://example.com/{index}")
                for index in range(1, 11)
            ))

        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_search_searxng", side_effect=upstream),
        ):
            small = asyncio.create_task(search.web_search("OpenAI", 1))
            await asyncio.wait_for(small_started.wait(), 1)
            rich = await search.web_search("OpenAI", 10)
            release_small.set()
            await small
            cached = await search.web_search("OpenAI", 10)

        self.assertEqual(2, calls)
        self.assertEqual(10, rich.count("URL: "))
        self.assertEqual(10, cached.count("URL: "))

    async def test_global_upstream_semaphore_bounds_distinct_queries(self):
        active = 0
        peak = 0
        release = asyncio.Event()
        two_started = asyncio.Event()

        async def upstream(query, max_results, timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            await release.wait()
            active -= 1
            return batch(result(query, f"https://example.com/{query}"))

        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_UPSTREAM_CONCURRENCY", 2),
            patch.object(search, "_search_searxng", side_effect=upstream),
        ):
            tasks = [
                asyncio.create_task(search.web_search(f"Query{index}", 1))
                for index in range(5)
            ]
            await asyncio.wait_for(two_started.wait(), 1)
            await asyncio.sleep(0)
            self.assertEqual(2, peak)
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(2, peak)

    async def test_local_semaphore_queue_timeout_does_not_open_circuit(self):
        with patch.object(search, "_UPSTREAM_CONCURRENCY", 1):
            blocker = search._upstream_semaphore()
            await blocker.acquire()
            try:
                with (
                    patch.object(
                        search.settings,
                        "web_search_providers",
                        "searxng",
                    ),
                    patch.object(search, "_CIRCUIT_FAILURE_THRESHOLD", 1),
                ):
                    for index in range(3):
                        attempts: list[str] = []
                        task = asyncio.create_task(search._fetch_provider(
                            "searxng",
                            f"Queued query {index}",
                            1.0,
                            attempts,
                        ))
                        await asyncio.sleep(0)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        self.assertIn(
                            "local concurrency queue deadline before dispatch",
                            " ".join(attempts),
                        )
            finally:
                blocker.release()

        state = search._CIRCUITS.get(search._endpoint_key("searxng"))
        self.assertTrue(state is None or state.failures == 0)
        self.assertTrue(state is None or state.opened_until == 0.0)

    async def test_endpoint_circuit_opens_then_half_open_success_closes_it(self):
        now = [1000.0]
        upstream = AsyncMock(side_effect=[
            httpx.ReadTimeout("one"),
            httpx.ReadTimeout("two"),
            batch(result("OpenAI recovery", "https://openai.com/recovery")),
            batch(result("OpenAI healthy", "https://openai.com/healthy")),
        ])
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_CIRCUIT_FAILURE_THRESHOLD", 2),
            patch.object(search, "_CIRCUIT_COOLDOWN_SECONDS", 10),
            patch.object(search, "_monotonic", side_effect=lambda: now[0]),
            patch.object(search, "_search_searxng", upstream),
        ):
            await search.web_search("OpenAI one", 1)
            await search.web_search("OpenAI two", 1)
            blocked = json.loads(await search.web_search("OpenAI three", 1))
            self.assertIn("circuit open", " ".join(blocked["attempts"]))
            self.assertEqual(2, upstream.await_count)
            now[0] += 11
            recovered = await search.web_search("OpenAI four", 1)
            healthy = await search.web_search("OpenAI five", 1)
        self.assertIn("OpenAI recovery", recovered)
        self.assertIn("OpenAI healthy", healthy)
        self.assertEqual(4, upstream.await_count)

    async def test_cross_engine_semantic_miss_does_not_open_endpoint_circuit(self):
        polluted = batch(
            result(
                "Unrelated football table",
                "https://junk.example/sport",
                engines=("baidu",),
            ),
            result(
                "Unrelated shopping coupons",
                "https://junk.example/shop",
                engines=("yahoo",),
            ),
            result(
                "Unrelated weather forecast",
                "https://junk.example/weather",
                engines=("mojeek",),
            ),
        )
        upstream = AsyncMock(side_effect=[
            polluted,
            batch(result("OpenAI recovered", "https://openai.com/research")),
        ])
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_CIRCUIT_FAILURE_THRESHOLD", 1),
            patch.object(search, "_search_searxng", upstream),
        ):
            first = json.loads(await search.web_search("OpenAI first", 1))
            second = await search.web_search("OpenAI second", 1)

        self.assertEqual("error", first["status"])
        self.assertIn("without opening", " ".join(first["attempts"]))
        self.assertIn("OpenAI recovered", second)
        self.assertEqual(2, upstream.await_count)

    async def test_single_engine_semantic_misses_never_open_endpoint_circuit(self):
        polluted = batch(
            result(
                "Unrelated football table",
                "https://junk.example/sport",
                engines=("bing",),
            ),
            result(
                "Unrelated shopping coupons",
                "https://junk.example/shop",
                engines=("bing",),
            ),
            result(
                "Unrelated weather forecast",
                "https://junk.example/weather",
                engines=("bing",),
            ),
        )
        upstream = AsyncMock(side_effect=[
            polluted,
            polluted,
            polluted,
            batch(result(
                "Galectin-3 recovered",
                "https://example.com/galectin-3",
            )),
        ])
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_CIRCUIT_FAILURE_THRESHOLD", 1),
            patch.object(search, "_search_searxng", upstream),
        ):
            for index in range(3):
                miss = json.loads(await search.web_search(
                    f"Legitimate niche query {index}", 1
                ))
                self.assertIn("without opening", " ".join(miss["attempts"]))
            recovered = await search.web_search("Galectin-3", 1)

        self.assertIn("Galectin-3 recovered", recovered)
        self.assertEqual(4, upstream.await_count)
        state = search._CIRCUITS[search._endpoint_key("searxng")]
        self.assertEqual(0, state.failures)
        self.assertEqual(0.0, state.opened_until)

    async def test_challenge_content_does_not_open_endpoint_circuit(self):
        challenge = batch(
            result(
                "One more step",
                "https://challenges.cloudflare.com/turnstile",
                "Verify you are human",
                engines=("bing",),
            )
        )
        upstream = AsyncMock(side_effect=[
            challenge,
            batch(result("OpenAI recovered", "https://openai.com/research")),
        ])
        with (
            patch.object(search.settings, "web_search_providers", "searxng"),
            patch.object(search, "_CIRCUIT_FAILURE_THRESHOLD", 1),
            patch.object(search, "_search_searxng", upstream),
        ):
            first = json.loads(await search.web_search("OpenAI one", 1))
            second = await search.web_search("OpenAI two", 1)

        self.assertIn("challenge/access-denied", " ".join(first["attempts"]))
        self.assertIn("OpenAI recovered", second)
        self.assertEqual(2, upstream.await_count)

    async def test_total_live_search_budget_bounds_the_whole_provider_chain(self):
        async def wedged_provider(provider, query, timeout, attempts):
            await asyncio.sleep(1)

        started = asyncio.get_running_loop().time()
        with patch.object(search, "_fetch_provider", side_effect=wedged_provider):
            outcome = await search._perform_search(
                "OpenAI",
                5,
                0.02,
                ("searxng", "ddg"),
                "fixture-key",
                None,
            )
        elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 0.25)
        self.assertTrue(outcome.timed_out)
        self.assertIn("total live-search timeout", " ".join(outcome.attempts))

    async def test_partial_success_after_provider_failure_has_short_retry_ttl(self):
        now = [1000.0]
        searx = AsyncMock(side_effect=[
            batch(result("OpenAI partial v1", "https://example.com/v1")),
            batch(result("OpenAI partial v2", "https://example.com/v2")),
        ])
        ddg = AsyncMock(side_effect=search._ProviderFailure("fixture unavailable"))
        with (
            patch.object(search.settings, "web_search_providers", "searxng,ddg"),
            patch.object(search, "_monotonic", side_effect=lambda: now[0]),
            patch.object(search, "_search_searxng", searx),
            patch.object(search, "_search_ddg", ddg),
        ):
            first = await search.web_search("OpenAI", 5)
            entry = next(iter(search._CACHE.values()))
            self.assertFalse(entry.complete)
            self.assertTrue(entry.degraded)
            self.assertLessEqual(entry.fresh_until - now[0], 60.0)
            now[0] += 61
            second = await search.web_search("OpenAI", 5)
        self.assertIn("partial v1", first)
        self.assertIn("partial v2", second)
        self.assertEqual(2, searx.await_count)

    async def test_empty_query_returns_actionable_error_without_upstream(self):
        upstream = AsyncMock()
        with patch.object(search, "_search_searxng", upstream):
            payload = json.loads(await search.web_search("   "))
        self.assertEqual("error", payload["status"])
        self.assertIn("must not be empty", payload["error"])
        upstream.assert_not_awaited()


class DuckDuckGoQueryTests(unittest.TestCase):
    def test_ordinary_query_is_not_prefixed_with_the_current_date(self):
        self.assertEqual(
            "Galectin-3 Alzheimer disease",
            search._dated_query("Galectin-3 Alzheimer disease"),
        )

    def test_explicitly_fresh_query_gets_an_iso_date_prefix(self):
        with patch.object(search, "date") as fake_date:
            fake_date.today.return_value.isoformat.return_value = "2026-07-21"
            self.assertEqual(
                "2026-07-21 latest OpenAI news",
                search._dated_query("latest OpenAI news"),
            )

    def test_ddg_client_receives_ordinary_query_verbatim(self):
        captured = []

        class FakeDDGS:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def text(self, query, **kwargs):
                captured.append((query, kwargs))
                return []

        module = types.SimpleNamespace(DDGS=FakeDDGS)
        with patch.dict(sys.modules, {"ddgs": module}):
            search._ddg_search_sync("OpenAI research", 5, search._BACKENDS[0])
        self.assertEqual("OpenAI research", captured[0][0])
        self.assertEqual("duckduckgo", captured[0][1]["backend"])


class DuckDuckGoBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        search._reset_search_state_for_tests()

    async def asyncTearDown(self):
        search._reset_search_state_for_tests()

    async def test_default_ddg_fallback_issues_one_supported_backend_request(self):
        backends = []

        def fake_sync(query, max_results, backend):
            backends.append(backend)
            return [{
                "title": "OpenAI research",
                "href": "https://openai.com/research",
                "body": "OpenAI research",
            }]

        with patch.object(search, "_ddg_search_sync", side_effect=fake_sync):
            actual = await search._search_ddg("OpenAI", 5, 1, [])
        self.assertEqual(["duckduckgo"], backends)
        self.assertEqual(1, len(actual.results))

    async def test_polluted_ddg_backend_continues_to_next_distinct_backend(self):
        backends = []

        def fake_sync(query, max_results, backend):
            backends.append(backend)
            if backend == "duckduckgo":
                return [{
                    "title": "Unrelated football result",
                    "href": "https://junk.example/sport",
                    "body": "scores and league table",
                }]
            return [{
                "title": "OpenAI research",
                "href": "https://openai.com/research",
                "body": "OpenAI research",
            }]

        with (
            patch.object(search, "_BACKENDS", ("duckduckgo", "mojeek")),
            patch.object(search, "_ddg_search_sync", side_effect=fake_sync),
        ):
            actual = await search._search_ddg("OpenAI", 5, 1, [])
        self.assertEqual(["duckduckgo", "mojeek"], backends)
        self.assertEqual("OpenAI research", actual.results[0].title)


if __name__ == "__main__":
    unittest.main()
