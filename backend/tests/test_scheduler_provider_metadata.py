import json
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from unittest.mock import patch

import scheduler
from scheduler import (
    _execute_claude_scheduled_turn,
    _harness_client,
    _job_may_run,
    _provider_payload,
    _resolve_job_model,
)


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

    async def test_claude_schedule_reuses_conversation_engine_projection(self):
        user = SimpleNamespace(id="u")
        agent_run = SimpleNamespace(
            source="cron",
            # _chat_stream creates this row in the caller's Session before
            # the detached terminal projector commits through another
            # Session.  Reproduce the stale identity-map value retained by
            # the scheduling Session after that external commit.
            status="running",
            error=None,
            finish_reason="stop",
            resolved_model_id="renamed-model",
            requested_model_id="renamed-model",
            input_tokens=3,
            output_tokens=4,
            total_tokens=7,
        )

        class Database:
            async def get(self, model, identity):
                return user if model.__name__ == "User" else agent_run

            async def rollback(self):
                return None

            async def refresh(self, value):
                if value is agent_run:
                    value.status = "succeeded"
                return None

        async def chunks():
            yield 'data: {"run_id":"agent-run","conversation_id":"session"}\n\n'
            yield 'data: {"delta":"scheduled output","run_id":"agent-run"}\n\n'

        response = SimpleNamespace(body_iterator=chunks())
        chat_stream = AsyncMock(return_value=response)
        job = SimpleNamespace(
            id="job", user_id="u", prompt="Read two factory sensors.",
            model_id="renamed-model", last_status=None, consecutive_errors=0,
        )
        conv = SimpleNamespace(id="session")
        scheduled_run = SimpleNamespace(
            id="scheduled-run", status="running", error=None, output=None,
            model_id=None, input_tokens=0, output_tokens=0, total_tokens=0,
            ended_at=None,
        )
        with (
            patch("routers.chat_router._chat_stream", chat_stream),
            patch.object(
                scheduler, "_commit_scheduled_session_state",
                AsyncMock(return_value=None),
            ),
            patch.object(scheduler, "emit_event", AsyncMock(return_value=None)),
            patch.object(scheduler, "_harness_client") as legacy_client,
        ):
            await _execute_claude_scheduled_turn(
                Database(),
                job=job,
                conv=conv,
                scheduled_run=scheduled_run,
                tools=["web_search"],
            )

        legacy_client.assert_not_called()
        self.assertEqual(chat_stream.await_args.kwargs["source"], "cron")
        self.assertEqual(
            chat_stream.await_args.kwargs["enabled_tools_override"],
            ("web_search",),
        )
        self.assertEqual(scheduled_run.status, "succeeded")
        self.assertEqual(scheduled_run.output, "scheduled output")

    def test_bounded_schedule_cannot_be_forced_past_max_runs(self):
        job = SimpleNamespace(
            max_runs=12,
            run_count=12,
            expires_at=None,
            enabled=True,
            next_run_at=scheduler._utcnow(),
        )
        flight = SimpleNamespace(force_requested=True)
        self.assertFalse(_job_may_run(job, flight))


if __name__ == "__main__":
    unittest.main()
