import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from agent_loop import _apply_provider_thinking_mode, _judge_goal, run_stream
from config import (
    DEFAULT_AGENT_MODEL_ID,
    PROVIDER_ALIASES,
    PROVIDERS,
)
from model_routing import resolve_agentic_model_routing


PRIMARY = {
    **PROVIDERS[DEFAULT_AGENT_MODEL_ID],
    "id": DEFAULT_AGENT_MODEL_ID,
}
QWEN = {
    **PROVIDERS["qwen3_5"],
    "id": "qwen3_5",
}


class AgenticModelRoutingDecisionTests(unittest.TestCase):
    def test_default_remote_model_does_not_rebind_historic_alias(self):
        self.assertEqual("shaiengine_glm_5_2", DEFAULT_AGENT_MODEL_ID)
        self.assertEqual("glm-5.2", PRIMARY["api_model"])
        self.assertEqual(1_000_000, PRIMARY["context_length"])
        self.assertEqual(86_400, PRIMARY["max_output_tokens"])
        self.assertEqual("deepseek_v4_pro", PROVIDER_ALIASES["AgentModel"])

    def _resolve(self, **overrides):
        kwargs = {
            "requested_model_id": "qwen3_5",
            "requested_provider": QWEN,
            "fallback_providers": (),
            "primary_provider": PRIMARY,
            "agent_kind": "primary",
            "execution_mode": "skill_workflow",
            "has_image_input": False,
            "effective_tools": (),
            "auxiliary_provider_ids": ("qwen3_5",),
        }
        kwargs.update(overrides)
        return resolve_agentic_model_routing(**kwargs)

    def test_skill_workflow_normalizes_qwen_to_primary(self):
        decision = self._resolve()
        self.assertTrue(decision.normalized)
        self.assertEqual(DEFAULT_AGENT_MODEL_ID, decision.effective_provider_id)
        self.assertEqual(PRIMARY["api_model"], decision.provider["api_model"])
        self.assertEqual(
            PRIMARY["max_output_tokens"],
            decision.provider["max_output_tokens"],
        )
        self.assertEqual(
            "agentic_or_skill_workflow_requires_primary_model",
            decision.reason,
        )

    def test_delegate_always_normalizes_qwen_to_primary(self):
        decision = self._resolve(
            agent_kind="delegate",
            execution_mode="delegated_subtask",
            has_image_input=True,
        )
        self.assertTrue(decision.normalized)
        self.assertEqual(
            "delegated_agent_requires_primary_model", decision.reason
        )

    def test_explicit_tools_closed_direct_image_keeps_qwen(self):
        decision = self._resolve(
            execution_mode="direct_chat",
            has_image_input=True,
        )
        self.assertFalse(decision.normalized)
        self.assertEqual("qwen3_5", decision.effective_provider_id)
        self.assertTrue(decision.explicit_direct_multimodal_auxiliary)

    def test_direct_image_with_tool_authority_uses_primary(self):
        decision = self._resolve(
            execution_mode="direct_chat",
            has_image_input=True,
            effective_tools=("web_search",),
        )
        self.assertTrue(decision.normalized)
        self.assertEqual("tool_using_turn_requires_primary_model", decision.reason)

    def test_qwen_is_removed_from_agentic_fallbacks_without_role_field(self):
        unmarked_qwen = {
            key: value
            for key, value in QWEN.items()
            if key != "agentic_auxiliary_only"
        }
        decision = self._resolve(
            requested_model_id=DEFAULT_AGENT_MODEL_ID,
            requested_provider=PRIMARY,
            fallback_providers=(unmarked_qwen,),
        )
        self.assertFalse(decision.normalized)
        self.assertEqual((), decision.fallback_providers)
        self.assertEqual(
            ("qwen3_5",), decision.filtered_fallback_provider_ids
        )
        self.assertEqual(
            "auxiliary_agentic_fallbacks_filtered", decision.reason
        )

    def test_audit_payload_never_contains_endpoint_or_key(self):
        payload = self._resolve().audit_payload()
        encoded = json.dumps(payload)
        self.assertNotIn("base_url", encoded)
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("10.10.", encoded)


class AgenticModelRoutingRunStreamTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _response_lines():
        return [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "ok"},
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            }),
            "data: [DONE]",
        ]

    async def _run(self, messages, *, provider=QWEN, max_tokens=None):
        requests = []

        class FakeResponse:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aiter_lines(self):
                for line in AgenticModelRoutingRunStreamTests._response_lines():
                    yield line
                    if isinstance(line, str) and line.startswith("data"):
                        yield ""

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, **kwargs):
                requests.append({"url": url, "body": kwargs.get("json")})
                return FakeResponse()

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.get_schemas", Mock(return_value=[])),
                patch("agent_loop.build_system_prompt", Mock(return_value="system")),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("agent_loop.settings.agent_debug_trace", True),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "qwen3_5",
                        messages,
                        [],
                        provider_override=provider,
                        allow_session_mcp=False,
                        user_id="u-model-routing",
                        session_id="s-model-routing",
                        max_iterations=1,
                        max_tokens=max_tokens,
                    )
                ]
        return requests, events

    async def test_plain_qwen_primary_is_normalized_before_first_request(self):
        requests, events = await self._run([
            {"role": "user", "content": "Explain deterministic routing."},
        ])
        model_requests = [
            request
            for request in requests
            if isinstance(request.get("body"), dict)
            and request["url"].endswith("/chat/completions")
        ]
        self.assertEqual(PRIMARY["api_model"], model_requests[0]["body"]["model"])
        self.assertEqual(
            {"type": "enabled"},
            model_requests[0]["body"]["thinking"],
        )
        self.assertNotIn("chat_template_kwargs", model_requests[0]["body"])
        started = next(
            event for event in events
            if event.get("event_type") == "run.started"
        )
        routing = started["payload"]["model_routing"]
        self.assertEqual("qwen3_5", routing["requested_provider_id"])
        self.assertEqual(DEFAULT_AGENT_MODEL_ID, routing["effective_provider_id"])
        self.assertTrue(routing["normalized"])
        self.assertTrue(any(
            event.get("event_type") == "model.switch"
            and event.get("payload", {}).get("routing_boundary") == "preflight"
            for event in events
        ))
        self.assertTrue(any(
            event.get("event_type") == "debug.model.routing"
            for event in events
        ))
        usage = next(event for event in events if event.get("type") == "usage")
        self.assertEqual(DEFAULT_AGENT_MODEL_ID, usage["model"])

    async def test_model_switch_applies_target_completion_cap_not_source_cap(self):
        requests, _events = await self._run(
            [{"role": "user", "content": "Run a bounded agentic task."}],
            max_tokens=120_000,
        )
        model_request = next(
            request
            for request in requests
            if isinstance(request.get("body"), dict)
            and request["url"].endswith("/chat/completions")
        )

        self.assertEqual(PRIMARY["api_model"], model_request["body"]["model"])
        self.assertEqual(
            PRIMARY["max_output_tokens"],
            model_request["body"]["max_tokens"],
        )

    async def test_explicit_direct_image_qwen_reaches_qwen(self):
        requests, events = await self._run([{
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }])
        model_requests = [
            request
            for request in requests
            if isinstance(request.get("body"), dict)
            and request["url"].endswith("/chat/completions")
        ]
        self.assertEqual("qwen3_5", model_requests[0]["body"]["model"])
        self.assertEqual(
            {"enable_thinking": False},
            model_requests[0]["body"]["chat_template_kwargs"],
        )
        started = next(
            event for event in events
            if event.get("event_type") == "run.started"
        )
        routing = started["payload"]["model_routing"]
        self.assertFalse(routing["normalized"])
        self.assertTrue(routing["explicit_direct_multimodal_auxiliary"])
        self.assertFalse(any(
            event.get("event_type") == "model.switch" for event in events
        ))


class GoalJudgeRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_goal_judge_uses_primary_agent_model(self):
        requests = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"status":"continue","reason":"more work"}'
                            )
                        }
                    }]
                }

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, **kwargs):
                requests.append({"url": url, "body": kwargs.get("json")})
                return FakeResponse()

        with patch("agent_loop.httpx.AsyncClient", FakeAsyncClient):
            status, reason, parse_failed = await _judge_goal(
                "Finish the implementation.",
                "The implementation is still incomplete.",
            )
        self.assertEqual("continue", status)
        self.assertEqual("more work", reason)
        self.assertFalse(parse_failed)
        self.assertEqual(PRIMARY["api_model"], requests[0]["body"]["model"])
        self.assertEqual(
            {"type": "disabled"},
            requests[0]["body"]["thinking"],
        )


class ThinkingRequestProjectionTests(unittest.TestCase):
    def test_projects_vllm_chat_template_toggle(self):
        body = {}
        self.assertTrue(_apply_provider_thinking_mode(
            body,
            {
                "supports_thinking_toggle": True,
                "thinking_request_format": "chat_template_kwargs",
            },
            enabled=False,
        ))
        self.assertEqual(
            {"chat_template_kwargs": {"enable_thinking": False}},
            body,
        )

    def test_projects_openai_thinking_object(self):
        body = {}
        self.assertTrue(_apply_provider_thinking_mode(
            body,
            {
                "supports_thinking_toggle": True,
                "thinking_request_format": "thinking_object",
            },
            enabled=True,
        ))
        self.assertEqual({"thinking": {"type": "enabled"}}, body)

    def test_unknown_thinking_wire_format_fails_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported provider thinking_request_format",
        ):
            _apply_provider_thinking_mode(
                {},
                {
                    "supports_thinking_toggle": True,
                    "thinking_request_format": "ambient_magic",
                },
                enabled=False,
            )


if __name__ == "__main__":
    unittest.main()
