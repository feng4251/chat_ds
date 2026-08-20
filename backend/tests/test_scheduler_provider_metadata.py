import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from unittest.mock import patch

import scheduler
from scheduler import (
    _execute_claude_scheduled_turn,
    _job_may_run,
    _scheduled_job_platform_capabilities,
)


class SchedulerProviderMetadataTests(unittest.IsolatedAsyncioTestCase):
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
        ):
            await _execute_claude_scheduled_turn(
                Database(),
                job=job,
                conv=conv,
                scheduled_run=scheduled_run,
                platform_capabilities=["web_search"],
            )

        self.assertEqual(chat_stream.await_args.kwargs["source"], "cron")
        self.assertEqual(
            chat_stream.await_args.kwargs["platform_capabilities_override"],
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

    def test_explicit_empty_scheduled_authority_is_not_defaulted(self):
        self.assertEqual(
            _scheduled_job_platform_capabilities(
                SimpleNamespace(enabled_tools="[]")
            ),
            [],
        )
        self.assertTrue(
            _scheduled_job_platform_capabilities(
                SimpleNamespace(enabled_tools=None)
            )
        )


if __name__ == "__main__":
    unittest.main()
