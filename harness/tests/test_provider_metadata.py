import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

import provider_metadata


class _ChunkedByteStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes, chunk_size: int = 64):
        self.payload = payload
        self.chunk_size = chunk_size

    async def __aiter__(self):
        for offset in range(0, len(self.payload), self.chunk_size):
            await asyncio.sleep(0)
            yield self.payload[offset:offset + self.chunk_size]

    async def aclose(self) -> None:
        return None


class ProviderRuntimeMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        provider_metadata.clear_provider_metadata_cache()

    def tearDown(self) -> None:
        provider_metadata.clear_provider_metadata_cache()

    @staticmethod
    def _provider(**overrides):
        return {
            "id": "primary",
            "api_model": "AgentModel",
            "base_url": "http://provider.test/v1",
            "api_key": "secret-not-for-audit",
            "protocol": "openai",
            "context_length": 303_872,
            "discover_runtime_metadata": True,
            **overrides,
        }

    async def test_exact_model_runtime_context_replaces_stale_static_value(self):
        real_client = httpx.AsyncClient

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual("/v1/models", request.url.path)
            return httpx.Response(200, json={
                "object": "list",
                "data": [
                    {"id": "other", "max_model_len": 999_999},
                    {"id": "AgentModel", "max_model_len": 250_368},
                ],
            })

        def client_factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler))

        with patch.object(provider_metadata.httpx, "AsyncClient", client_factory):
            resolved, audit = (
                await provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                )
            )

        self.assertEqual(250_368, resolved["context_length"])
        self.assertEqual("runtime_catalog", audit["status"])
        self.assertTrue(audit["metadata_applied"])
        encoded = repr(audit)
        self.assertNotIn("provider.test", encoded)
        self.assertNotIn("secret-not-for-audit", encoded)

    async def test_missing_exact_model_falls_back_without_taking_first_record(self):
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "data": [{"id": "different", "max_model_len": 1_024}],
            })

        def client_factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler))

        with patch.object(provider_metadata.httpx, "AsyncClient", client_factory):
            resolved, audit = (
                await provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                )
            )

        self.assertEqual(303_872, resolved["context_length"])
        self.assertEqual("model_not_found", audit["status"])
        self.assertFalse(audit["metadata_applied"])

    async def test_unexpected_client_exception_fails_open(self):
        class ClientWithoutGet:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with patch.object(
            provider_metadata.httpx,
            "AsyncClient",
            return_value=ClientWithoutGet(),
        ):
            resolved, audit = (
                await provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                )
            )

        self.assertEqual(303_872, resolved["context_length"])
        self.assertEqual("catalog_unavailable", audit["status"])
        self.assertFalse(audit["metadata_applied"])

    async def test_concurrent_resolution_is_singleflight_and_cached(self):
        real_client = httpx.AsyncClient
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json={
                "data": [{"id": "AgentModel", "context_window": 250_368}],
            })

        def client_factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler))

        with patch.object(provider_metadata.httpx, "AsyncClient", client_factory):
            first, second = await asyncio.gather(
                provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                ),
                provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                ),
            )
            third = await provider_metadata.resolve_provider_runtime_metadata(
                self._provider()
            )

        self.assertEqual(1, calls)
        self.assertEqual(250_368, first[0]["context_length"])
        self.assertEqual(250_368, second[0]["context_length"])
        self.assertTrue(str(third[1]["status"]).startswith("cache:"))

    async def test_provider_error_feedback_tightens_cached_context(self):
        provider = self._provider(discover_runtime_metadata=False)
        provider_metadata.record_provider_context_limit(provider, 250_368)
        provider["discover_runtime_metadata"] = True

        resolved, audit = (
            await provider_metadata.resolve_provider_runtime_metadata(provider)
        )

        self.assertEqual(250_368, resolved["context_length"])
        self.assertEqual("cache:provider_error_feedback", audit["status"])

    async def test_cancelled_waiter_does_not_leak_completed_inflight_task(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_fetch(provider):
            entered.set()
            await release.wait()
            return {"context_length": 250_368}, "runtime_catalog"

        with patch.object(
            provider_metadata,
            "_fetch_provider_metadata",
            side_effect=delayed_fetch,
        ):
            waiter = asyncio.create_task(
                provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                )
            )
            await entered.wait()
            self.assertEqual(1, len(provider_metadata._INFLIGHT))
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()
            for _ in range(4):
                await asyncio.sleep(0)

        self.assertEqual({}, provider_metadata._INFLIGHT)

    async def test_inflight_catalog_cannot_loosen_provider_error_feedback(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        provider = self._provider()

        async def stale_fetch(candidate):
            entered.set()
            await release.wait()
            return {"context_length": 303_872}, "runtime_catalog"

        with patch.object(
            provider_metadata,
            "_fetch_provider_metadata",
            side_effect=stale_fetch,
        ):
            resolver = asyncio.create_task(
                provider_metadata.resolve_provider_runtime_metadata(provider)
            )
            await entered.wait()
            provider_metadata.record_provider_context_limit(provider, 250_368)
            release.set()
            resolved, audit = await resolver
            cached, cached_audit = (
                await provider_metadata.resolve_provider_runtime_metadata(
                    provider
                )
            )

        self.assertEqual(250_368, resolved["context_length"])
        self.assertIn("provider_error_feedback", audit["status"])
        self.assertEqual(250_368, cached["context_length"])
        self.assertIn("provider_error_feedback", cached_audit["status"])

    async def test_declared_oversized_catalog_fails_open_without_body_read(self):
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Length": str(
                        provider_metadata._MAX_CATALOG_BYTES + 1
                    )
                },
                content=b"",
            )

        def client_factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler))

        with patch.object(provider_metadata.httpx, "AsyncClient", client_factory):
            resolved, audit = (
                await provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                )
            )

        self.assertEqual(303_872, resolved["context_length"])
        self.assertEqual("catalog_too_large", audit["status"])
        self.assertFalse(audit["metadata_applied"])

    async def test_streamed_oversized_catalog_is_singleflight_and_cached(self):
        real_client = httpx.AsyncClient
        calls = 0
        payload = json.dumps({
            "data": [{
                "id": "AgentModel",
                "max_model_len": 250_368,
                "padding": "x" * 512,
            }],
        }).encode("utf-8")

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                stream=_ChunkedByteStream(payload),
            )

        def client_factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler))

        with (
            patch.object(provider_metadata, "_MAX_CATALOG_BYTES", 128),
            patch.object(provider_metadata.httpx, "AsyncClient", client_factory),
        ):
            first, second = await asyncio.gather(
                provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                ),
                provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                ),
            )
            cached = await provider_metadata.resolve_provider_runtime_metadata(
                self._provider()
            )

        self.assertEqual(1, calls)
        self.assertEqual("catalog_too_large", first[1]["status"])
        self.assertEqual("catalog_too_large", second[1]["status"])
        self.assertEqual(
            "cache:catalog_too_large",
            cached[1]["status"],
        )
        self.assertEqual(303_872, cached[0]["context_length"])

    async def test_record_count_limit_fails_open(self):
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "data": [
                    {"id": "AgentModel", "max_model_len": 250_368},
                    {"id": "other-one", "max_model_len": 250_368},
                    {"id": "other-two", "max_model_len": 250_368},
                ],
            })

        def client_factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler))

        with (
            patch.object(provider_metadata, "_MAX_MODEL_RECORDS", 2),
            patch.object(provider_metadata.httpx, "AsyncClient", client_factory),
        ):
            resolved, audit = (
                await provider_metadata.resolve_provider_runtime_metadata(
                    self._provider()
                )
            )

        self.assertEqual(303_872, resolved["context_length"])
        self.assertEqual("catalog_record_limit_exceeded", audit["status"])
        self.assertFalse(audit["metadata_applied"])


if __name__ == "__main__":
    unittest.main()
