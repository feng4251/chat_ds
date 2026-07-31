import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from agent_loop import run_stream
from tests.support.scripted_provider import (
    ScriptedProvider,
    interrupted_turn,
    stop_turn,
)


class ScriptedProviderContractMatrixTests(
    unittest.IsolatedAsyncioTestCase
):
    provider_config = {
        "id": "scripted-generic",
        "base_url": "http://scripted.invalid/v1",
        "api_model": "scripted-generic",
        "api_key": "EMPTY",
        "protocol": "openai",
        "provider": "scripted",
        "context_length": 64_000,
        "is_multimodal": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
    }

    async def _run(self, scripted: ScriptedProvider):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch(
                    "agent_loop.httpx.AsyncClient",
                    scripted.client_factory,
                ),
                patch(
                    "agent_loop.build_system_prompt",
                    return_value="generic harness system",
                ),
                patch(
                    "agent_loop.load_workspace_context",
                    return_value="",
                ),
                patch(
                    "agent_loop._fetch_goal",
                    AsyncMock(return_value=None),
                ),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[],
                ),
                patch(
                    "workspace_context.WORKSPACE_ROOT",
                    Path(temp_dir),
                ),
            ):
                events = [
                    event async for event in run_stream(
                        "scripted-generic",
                        [{
                            "role": "user",
                            "content": (
                                "Summarize the supplied generic observation."
                            ),
                        }],
                        [],
                        user_id="scripted-user",
                        session_id="scripted-session",
                        provider_override=self.provider_config,
                        source="chat",
                        agent_kind="primary",
                        max_iterations=3,
                    )
                ]
        scripted.assert_exhausted()
        return events

    async def test_clean_terminal_turn_has_one_request_and_one_terminal(
        self,
    ):
        def assert_request(body):
            self.assertEqual("scripted-generic", body["model"])
            self.assertEqual([], body.get("tools", []))
            self.assertTrue(body["stream"])

        scripted = ScriptedProvider([
            stop_turn(
                "The generic observation is internally consistent.",
                assert_request=assert_request,
            ),
        ])

        events = await self._run(scripted)

        self.assertEqual(1, len(scripted.requests))
        self.assertEqual(
            1,
            sum(
                event.get("event_type") == "run.completed"
                for event in events
            ),
        )
        self.assertFalse(any(
            event.get("event_type") in {
                "run.failed",
                "run.cancelled",
            }
            for event in events
        ))

    async def test_visible_stream_interruption_is_not_http_replayed(self):
        scripted = ScriptedProvider([
            interrupted_turn(
                "Partial generic observation",
                httpx.ReadError("scripted connection reset"),
            ),
        ])

        events = await self._run(scripted)

        self.assertEqual(1, len(scripted.requests))
        failed = [
            event
            for event in events
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(1, len(failed))
        self.assertIn(
            failed[0]["payload"].get("finish_reason"),
            {
                "stream_interrupted_after_partial",
                "provider_stream_interrupted",
            },
        )
        self.assertFalse(
            failed[0]["payload"].get("http_request_replayed", False)
        )
