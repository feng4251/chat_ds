import asyncio
import json
import unittest
from unittest.mock import patch

from tools.context import ToolContext
from tools.delegation import _dispatch_can_mutate, _run_child, delegate_task


def _context(*tools: str) -> ToolContext:
    return ToolContext(
        user_id="side-effect-user",
        session_id="side-effect-session",
        model_id="model",
        provider_config={
            "base_url": "http://example",
            "api_model": "model",
            "context_length": 303_872,
        },
        enabled_tools=tools,
        run_id="parent",
        root_run_id="root",
    )


def _started(tool_name: str, call_id: str) -> dict:
    return {
        "type": "agent_event",
        "event_type": "tool.started",
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "payload": {
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "args_compacted": {},
            "preflight_pending": True,
        },
    }


def _dispatch_started(tool_name: str, call_id: str) -> dict:
    return {
        "type": "agent_event",
        "event_type": "tool.dispatch_started",
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "payload": {
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "actual_dispatch_attempted": True,
        },
    }


def _completed(tool_name: str, call_id: str) -> dict:
    return {
        "type": "agent_event",
        "event_type": "tool.completed",
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "payload": {
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "outcome": "success",
            "actual_dispatch_attempted": True,
        },
    }


class DelegationSideEffectRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_exact_post_remains_mutating_and_nonretryable(self):
        self.assertTrue(_dispatch_can_mutate("skill_http_post_json"))
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            self.assertIn("skill_http_post_json", tools)
            call_id = "post-side-effect-boundary"
            yield _started("skill_http_post_json", call_id)
            yield _dispatch_started("skill_http_post_json", call_id)
            yield _completed("skill_http_post_json", call_id)
            # A bare completion acknowledgement is deterministically invalid
            # even though legitimate short free-prose results are now allowed.
            yield {"type": "delta", "content": "done"}
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch("tools.delegation.persist_result_for_history") as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "perform one exact declared JSON POST request",
                    "tools": ["skill_http_post_json"],
                },
                _context("skill_http_post_json"),
                0,
                parallel_child=True,
            )

        self.assertFalse(result["retryable"])
        self.assertEqual("agent_contract_noncompliance", result["failure_class"])
        self.assertEqual(
            ["skill_http_post_json"],
            result["dispatch_receipt_audit"]["mutating_tool_names"],
        )
        persist_result.assert_not_called()

    async def test_write_and_patch_then_invalid_output_are_nonretryable(self):
        for tool_name in ("write_file", "patch_file"):
            with self.subTest(tool=tool_name):
                async def fake_run_stream(model_id, messages, tools, **kwargs):
                    call_id = "mutating-boundary"
                    yield _started(tool_name, call_id)
                    yield _dispatch_started(tool_name, call_id)
                    yield _completed(tool_name, call_id)
                    yield {"type": "delta", "content": "done"}
                    yield {"type": "done", "finish_reason": "stop"}

                with (
                    patch("agent_loop.run_stream", fake_run_stream),
                    patch(
                        "tools.delegation.persist_result_for_history"
                    ) as persist_result,
                ):
                    result = await _run_child(
                        {
                            "goal": "perform one bounded artifact operation",
                            "tools": [tool_name],
                        },
                        _context(tool_name),
                        0,
                    )

                self.assertEqual(result["status"], "error")
                self.assertFalse(result["retryable"])
                self.assertEqual(
                    result["failure_class"],
                    "agent_contract_noncompliance",
                )
                self.assertEqual(
                    result["dispatch_receipt_audit"],
                    {
                        "dispatch_count": 1,
                        "mutating_dispatch_count": 1,
                        "read_only_dispatch_count": 0,
                        "tool_names": [tool_name],
                        "mutating_tool_names": [tool_name],
                        "read_only_tool_names": [],
                    },
                )
                persist_result.assert_not_called()

    async def test_read_only_dispatch_then_invalid_output_remains_retryable(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            call_id = "read-boundary"
            yield _started("read_file", call_id)
            yield _dispatch_started("read_file", call_id)
            yield _completed("read_file", call_id)
            yield {"type": "delta", "content": "done"}
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch("tools.delegation.persist_result_for_history") as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "inspect one bounded source",
                    "tools": ["read_file"],
                },
                _context("read_file"),
                0,
            )

        self.assertEqual(result["status"], "error")
        self.assertTrue(result["retryable"])
        self.assertEqual(
            result["dispatch_receipt_audit"]["read_only_dispatch_count"],
            1,
        )
        self.assertEqual(
            result["dispatch_receipt_audit"]["mutating_dispatch_count"],
            0,
        )
        persist_result.assert_not_called()

    async def test_batch_timeout_uses_parent_receipts_and_exact_step_ids(self):
        cancelled: set[str] = set()
        continued_after_cancel: set[str] = set()

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            prompt = str(messages[0].get("content") or "")
            if "wait after managed execution" in prompt:
                label = "mutating"
                tool_name = "execute_code"
            elif "wait after a read" in prompt:
                label = "read-only"
                tool_name = "read_file"
            else:
                label = "before-dispatch"
                tool_name = "read_file"

            try:
                started = _started(tool_name, "timeout-" + label)
                if label == "mutating":
                    started["payload"]["args_compacted"] = {
                        "code": "sentinel-secret-in-arguments",
                    }
                await kwargs["event_sink"](started)
                yield started
                if label != "before-dispatch":
                    # Capture the receipt through event_sink, then block before
                    # the iterator can yield it. This is the cancellation race
                    # the parent-owned tracker must close.
                    await kwargs["event_sink"](
                        _dispatch_started(tool_name, "timeout-" + label)
                    )
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(label)
                raise
            continued_after_cancel.add(label)
            if False:  # pragma: no cover - keeps this an async generator
                yield {"type": "done", "finish_reason": "stop"}

        tasks = [
            {
                "goal": "wait before any tool",
                "skill_name": "generic",
                "step_type": "knowledge_bootstrap",
                "step_id": "before",
                "tools": ["read_file"],
            },
            {
                "goal": "wait after a read",
                "skill_name": "generic",
                "step_type": "knowledge_bootstrap",
                "step_id": "reader",
                "tools": ["read_file"],
            },
            {
                "goal": "wait after managed execution",
                "skill_name": "generic",
                "step_type": "knowledge_bootstrap",
                "step_id": "executor",
                "tools": ["execute_code"],
            },
        ]
        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.settings.delegation_batch_timeout_seconds",
                0.5,
            ),
        ):
            payload = json.loads(await delegate_task(
                tasks=tasks,
                context=_context("read_file", "execute_code"),
            ))

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["retryable_failed_step_ids"], [
            "before",
            "reader",
        ], payload)
        self.assertEqual(payload["terminal_failed_step_ids"], ["executor"])
        self.assertEqual(
            [result["step_id"] for result in payload["results"]],
            ["before", "reader", "executor"],
        )

        before, reader, executor = payload["results"]
        self.assertTrue(before["retryable"])
        self.assertEqual(before["terminal_reason"], "delegated_child_timeout")
        self.assertEqual(
            before["dispatch_receipt_audit"]["dispatch_count"],
            0,
        )
        self.assertTrue(reader["retryable"])
        self.assertEqual(
            reader["dispatch_receipt_audit"]["read_only_tool_names"],
            ["read_file"],
        )
        self.assertFalse(executor["retryable"])
        self.assertEqual(
            executor["terminal_reason"],
            "delegated_child_timeout_after_mutating_dispatch",
        )
        self.assertEqual(
            executor["failure_class"],
            "side_effect_state_uncertain",
        )
        self.assertEqual(
            executor["dispatch_receipt_audit"]["mutating_tool_names"],
            ["execute_code"],
        )
        self.assertNotIn("sentinel-secret", json.dumps(payload))
        self.assertEqual(cancelled, {
            "before-dispatch",
            "read-only",
            "mutating",
        })
        await asyncio.sleep(0)
        self.assertEqual(continued_after_cancel, set())


if __name__ == "__main__":
    unittest.main()
