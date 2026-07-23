import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import scheduler
from scheduler import _harness_client, _provider_payload, _resolve_job_model


class SchedulerProviderMetadataTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_payload_preserves_runtime_discovery_policy(self):
        payload = _provider_payload(
            "primary",
            {
                "base_url": "http://provider.test/v1",
                "api_key": "unused-test-key",
                "api_model": "AgentModel",
                "context_length": 303_872,
                "discover_runtime_metadata": True,
            },
        )

        self.assertTrue(payload["discover_runtime_metadata"])

    async def test_custom_job_model_enables_runtime_discovery(self):
        custom = SimpleNamespace(
            model_id="custom-runtime-model",
            base_url="http://provider.test/v1/",
            api_key="unused-test-key",
            provider="openai",
            is_multimodal=False,
            extra_headers=json.dumps({"X-Test": "value"}),
        )

        class Result:
            def scalar_one_or_none(self):
                return custom

        class Database:
            async def execute(self, statement):
                return Result()

        resolved = await _resolve_job_model(
            Database(),
            "user",
            custom.model_id,
        )

        self.assertTrue(resolved["discover_runtime_metadata"])
        self.assertEqual("http://provider.test/v1", resolved["base_url"])

    def test_scheduler_reuses_shared_harness_timeout(self):
        with patch.object(scheduler.httpx, "AsyncClient") as client:
            _harness_client()

        client.assert_called_once_with(
            timeout=scheduler.settings.harness_stream_timeout_seconds
        )


if __name__ == "__main__":
    unittest.main()
