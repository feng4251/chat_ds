from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

import main as harness_main


class InternalAPIAuthTests(unittest.IsolatedAsyncioTestCase):
    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=harness_main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://harness.test",
        ) as client:
            return await client.request(method, path, **kwargs)

    async def test_stateful_routes_reject_missing_or_wrong_token(self) -> None:
        with patch.object(harness_main.settings, "internal_api_token", "opaque-test-token"):
            missing = await self._request(
                "POST",
                "/v1/chat/completions",
                json={"messages": [], "stream": False},
            )
            wrong = await self._request(
                "GET",
                "/internal/mcp/tools?user_id=u&session_id=s",
                headers={"X-Internal-Token": "wrong"},
            )

        self.assertEqual(401, missing.status_code)
        self.assertEqual(401, wrong.status_code)
        self.assertNotIn("opaque-test-token", missing.text)

    async def test_authorized_nonstream_chat_reaches_agent_loop(self) -> None:
        observed: dict = {}

        async def fake_run_stream(*_args, **_kwargs):
            observed.update(_kwargs)
            yield {"type": "delta", "content": "ok"}
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch.object(harness_main.settings, "internal_api_token", "opaque-test-token"),
            patch.object(harness_main, "run_stream", fake_run_stream),
        ):
            response = await self._request(
                "POST",
                "/v1/chat/completions",
                headers={"X-Internal-Token": "opaque-test-token"},
                json={
                    "messages": [],
                    "stream": False,
                    "run_metadata": {
                        "run_id": "run-1",
                        "root_run_id": "root-1",
                        "agent_kind": "primary",
                    },
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("ok", response.json()["choices"][0]["message"]["content"])
        self.assertEqual("run-1", observed["run_id"])
        self.assertEqual("root-1", observed["root_run_id"])

    async def test_health_and_model_catalog_remain_read_only_probes(self) -> None:
        with patch.object(
            harness_main,
            "storage_root_attestation",
            return_value={
                "version": 1,
                "available": True,
                "identity_sha256": "a" * 64,
            },
        ):
            health = await self._request("GET", "/health")
        models = await self._request("GET", "/v1/models")
        self.assertEqual(200, health.status_code)
        self.assertEqual(200, models.status_code)


if __name__ == "__main__":
    unittest.main()
