import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import HarnessRunState, run_stream
from tools.registry import dispatch as native_dispatch


def _stop_turn(content: str) -> list[str]:
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {"content": content},
                "finish_reason": None,
            }],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "stop"}],
        }),
        "data: [DONE]",
    ]


def _tool_batch_turn(arguments: list[dict]) -> list[str]:
    tool_calls = [
        {
            "index": index,
            "id": f"call-{index}",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        }
        for index, args in enumerate(arguments)
    ]
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {"tool_calls": tool_calls},
                "finish_reason": None,
            }],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }),
        "data: [DONE]",
    ]


class DeclaredWorkflowBatchAtomicityTests(unittest.IsolatedAsyncioTestCase):
    provider = {
        "id": "mock-workflow-batch",
        "base_url": "http://model.invalid/v1",
        "api_model": "mock-workflow-batch",
        "api_key": "EMPTY",
        "protocol": "openai",
        "provider": "mock",
        "context_length": 64_000,
        "is_multimodal": False,
    }

    async def _run_batch(self, arguments: list[dict]) -> dict:
        responses = [
            _stop_turn("I will execute the declared artifact step."),
            _tool_batch_turn(arguments),
        ]
        request_bodies: list[dict] = []

        class FakeResponse:
            status_code = 200

            def __init__(self, lines):
                self._lines = lines

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aiter_lines(self):
                for line in self._lines:
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
                request_bodies.append(kwargs.get("json"))
                if not responses:
                    raise AssertionError("unexpected extra model request")
                return FakeResponse(responses.pop(0))

        gate_call_count = 0

        def needs_more_workflow(_state):
            nonlocal gate_call_count
            gate_call_count += 1
            if gate_call_count == 1:
                return (
                    True,
                    "generate declared modular/checklist artifacts for "
                    "session skill 'generic'",
                )
            return False, ""

        dispatch_spy = AsyncMock(wraps=native_dispatch)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch("workspace_context.WORKSPACE_ROOT", root),
                patch("tools.path_security.SANDBOX_ROOT", root),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", dispatch_spy),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("skills.scanner.find_all_skills", return_value=[]),
                patch.object(
                    HarnessRunState,
                    "needs_more_skill_workflow",
                    needs_more_workflow,
                ),
                patch("agent_loop.settings.agent_debug_trace", True),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-workflow-batch",
                        [{
                            "role": "user",
                            "content": "Execute this Skill workflow exactly.",
                        }],
                        ["write_file"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-workflow-batch",
                        session_id="s-workflow-batch",
                        max_iterations=2,
                    )
                ]
            workspace = (
                root
                / "u-workflow-batch"
                / "s-workflow-batch"
                / "workspace"
            )
            files = {
                path.name: path.read_text(encoding="utf-8")
                for path in workspace.glob("*.md")
            } if workspace.exists() else {}

        return {
            "events": events,
            "dispatch": dispatch_spy,
            "files": files,
            "requests": request_bodies,
        }

    async def test_valid_first_and_invalid_or_omitted_second_are_atomic(self):
        invalid_second_calls = (
            {"filepath": "B.md"},
            {
                "filepath": "B.md",
                "content_omitted": {
                    "_chatds_argument_omitted": True,
                    "kind": "large_file_content",
                },
            },
        )
        for invalid_second in invalid_second_calls:
            with self.subTest(invalid_second=invalid_second):
                result = await self._run_batch([
                    {"filepath": "A.md", "content": "first"},
                    invalid_second,
                ])

                result["dispatch"].assert_not_awaited()
                self.assertEqual(result["files"], {})
                preflight_events = [
                    event for event in result["events"]
                    if event.get("event_type") == "debug.tool.batch_preflight"
                ]
                self.assertEqual(len(preflight_events), 1)
                audit = preflight_events[0]["payload"]
                self.assertFalse(audit["accepted"])
                self.assertEqual(audit["batch_size"], 2)
                self.assertEqual(audit["rejected_call_count"], 1)
                self.assertEqual(audit["atomic_abort_call_count"], 1)
                failed = [
                    event for event in result["events"]
                    if event.get("event_type") == "tool.failed"
                ]
                self.assertEqual(len(failed), 2)
                self.assertTrue(all(
                    event["payload"]["outcome"] == "error"
                    for event in failed
                ))

    async def test_all_valid_batch_preserves_declared_order(self):
        result = await self._run_batch([
            {"filepath": "A.md", "content": "first"},
            {"filepath": "B.md", "content": "second"},
        ])

        self.assertEqual(result["dispatch"].await_count, 2)
        self.assertEqual(
            [call.args[:2] for call in result["dispatch"].await_args_list],
            [
                ("write_file", {"filepath": "A.md", "content": "first"}),
                ("write_file", {"filepath": "B.md", "content": "second"}),
            ],
        )
        self.assertEqual(result["files"], {
            "A.md": "first",
            "B.md": "second",
        })
        preflight = next(
            event for event in result["events"]
            if event.get("event_type") == "debug.tool.batch_preflight"
        )
        self.assertTrue(preflight["payload"]["accepted"])
        self.assertEqual(preflight["payload"]["rejected_call_count"], 0)


if __name__ == "__main__":
    unittest.main()
