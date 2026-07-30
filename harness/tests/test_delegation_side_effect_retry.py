import asyncio
import json
import unittest
from unittest.mock import patch

from tools.context import ToolContext
from tools.delegation import _dispatch_can_mutate, _run_child, delegate_task
from tools.effect_receipt import (
    bind_effect_receipt_to_call,
    bound_effect_receipt_is_replay_safe,
    build_isolated_execution_effect_receipt,
)


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


def _completed(
    tool_name: str,
    call_id: str,
    *,
    effect_receipt: dict | None = None,
) -> dict:
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
            "effect_receipt": effect_receipt,
        },
    }


def _bound_effect_receipt(
    tool_name: str,
    call_id: str,
    *,
    methods: tuple[str, ...] = ("GET", "HEAD"),
    artifact_count: int = 0,
) -> dict:
    receipt = build_isolated_execution_effect_receipt(
        result={
            "status": "success",
            "artifacts": [
                {"path": f"artifact-{index}.txt"}
                for index in range(artifact_count)
            ],
        },
        egress_rules=[{
            "url_prefix": "https://api.example.test/data",
            "methods": list(methods),
        }],
        tool_operation_id="operation-id",
    )
    bound = bind_effect_receipt_to_call(
        receipt,
        tool_name=tool_name,
        tool_call_id=call_id,
    )
    assert bound is not None
    return bound


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
                        "replay_safe_mutating_dispatch_count": 0,
                        "unsafe_mutating_dispatch_count": 1,
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

    async def test_completed_read_only_script_receipt_is_retryable(self):
        call_id = "python-read-only-effect"

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _started("run_skill_python", call_id)
            yield _dispatch_started("run_skill_python", call_id)
            yield _completed(
                "run_skill_python",
                call_id,
                effect_receipt=_bound_effect_receipt(
                    "run_skill_python",
                    call_id,
                ),
            )
            yield {"type": "delta", "content": "done"}
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch("tools.delegation.persist_result_for_history"),
        ):
            result = await _run_child(
                {
                    "goal": "query one declared public evidence endpoint",
                    "tools": ["run_skill_python"],
                },
                _context("run_skill_python"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertTrue(result["retryable"])
        receipt = result["dispatch_receipt_audit"]
        self.assertEqual(1, receipt["mutating_dispatch_count"])
        self.assertEqual(1, receipt["replay_safe_mutating_dispatch_count"])
        self.assertEqual(0, receipt["unsafe_mutating_dispatch_count"])

    async def test_wrapper_exception_honors_completed_read_only_receipt(self):
        call_id = "python-read-only-before-wrapper-exception"

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            yield _started("run_skill_python", call_id)
            yield _dispatch_started("run_skill_python", call_id)
            yield _completed(
                "run_skill_python",
                call_id,
                effect_receipt=_bound_effect_receipt(
                    "run_skill_python",
                    call_id,
                ),
            )
            raise RuntimeError("synthetic wrapper failure")

        with patch("agent_loop.run_stream", fake_run_stream):
            payload = json.loads(await delegate_task(
                tasks=[{
                    "goal": "query one declared read-only endpoint",
                    "step_id": "read-query",
                    "tools": ["run_skill_python"],
                }],
                context=_context("run_skill_python"),
            ))

        self.assertEqual("error", payload["status"])
        result = payload["results"][0]
        self.assertTrue(result["retryable"])
        self.assertEqual(
            "delegated_child_exception",
            result["terminal_reason"],
        )
        self.assertEqual(
            1,
            result["dispatch_receipt_audit"][
                "replay_safe_mutating_dispatch_count"
            ],
        )
        self.assertEqual(
            0,
            result["dispatch_receipt_audit"][
                "unsafe_mutating_dispatch_count"
            ],
        )

    async def test_script_effect_receipt_fails_closed_for_write_or_post(self):
        cases = {
            "artifact": {
                "methods": ("GET",),
                "artifact_count": 1,
                "bound_call_id": "effect-artifact",
            },
            "post": {
                "methods": ("POST",),
                "artifact_count": 0,
                "bound_call_id": "effect-post",
            },
            "wrong_call": {
                "methods": ("GET",),
                "artifact_count": 0,
                "bound_call_id": "different-call",
            },
        }
        for label, case in cases.items():
            call_id = "effect-" + label

            async def fake_run_stream(
                model_id,
                messages,
                tools,
                *,
                _call_id=call_id,
                _case=case,
                **kwargs,
            ):
                yield _started("run_skill_python", _call_id)
                yield _dispatch_started("run_skill_python", _call_id)
                yield _completed(
                    "run_skill_python",
                    _call_id,
                    effect_receipt=_bound_effect_receipt(
                        "run_skill_python",
                        _case["bound_call_id"],
                        methods=_case["methods"],
                        artifact_count=_case["artifact_count"],
                    ),
                )
                yield {"type": "delta", "content": "done"}
                yield {"type": "done", "finish_reason": "stop"}

            with self.subTest(label=label):
                with (
                    patch("agent_loop.run_stream", fake_run_stream),
                    patch("tools.delegation.persist_result_for_history"),
                ):
                    result = await _run_child(
                        {
                            "goal": "run one bounded declared script",
                            "tools": ["run_skill_python"],
                        },
                        _context("run_skill_python"),
                        0,
                    )

            self.assertFalse(result["retryable"])
            receipt = result["dispatch_receipt_audit"]
            self.assertEqual(0, receipt[
                "replay_safe_mutating_dispatch_count"
            ])
            self.assertEqual(1, receipt["unsafe_mutating_dispatch_count"])

    def test_malformed_or_unknown_egress_methods_fail_closed(self):
        malformed_rules = (
            [{"url_prefix": "https://example.test", "methods": []}],
            [{"url_prefix": "https://example.test", "methods": ["TRACE"]}],
            [{"url_prefix": "https://example.test"}],
        )
        for index, rules in enumerate(malformed_rules):
            with self.subTest(rules=rules):
                receipt = build_isolated_execution_effect_receipt(
                    result={"status": "success", "artifacts": []},
                    egress_rules=rules,
                    tool_operation_id="operation-id",
                )
                self.assertFalse(receipt["effect_known"])
                self.assertFalse(receipt["replay_safe"])
                bound = bind_effect_receipt_to_call(
                    receipt,
                    tool_name="run_skill_python",
                    tool_call_id=f"malformed-{index}",
                )
                self.assertIsNotNone(bound)
                self.assertFalse(bound_effect_receipt_is_replay_safe(
                    bound,
                    tool_name="run_skill_python",
                    tool_call_id=f"malformed-{index}",
                ))

    def test_bound_effect_receipt_tampering_fails_closed(self):
        bound = _bound_effect_receipt(
            "run_skill_python",
            "tamper-boundary",
        )
        bound["artifact_commit_count"] = 1
        self.assertFalse(bound_effect_receipt_is_replay_safe(
            bound,
            tool_name="run_skill_python",
            tool_call_id="tamper-boundary",
        ))

    async def test_read_only_dispatch_then_plain_output_limit_is_retryable(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            call_id = "read-before-output-limit"
            yield _started("read_file", call_id)
            yield _dispatch_started("read_file", call_id)
            yield _completed("read_file", call_id)
            error = (
                "Delegated model hit its output limit; returning the bounded "
                "partial child payload for outer contract validation."
            )
            yield {"type": "delta", "content": "partial evidence"}
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": error,
                    "finish_reason": "length",
                    "terminal_reason": "model_hit_max_output_tokens",
                },
            }
            yield {"type": "error", "msg": error}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch("tools.delegation.persist_result_for_history") as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "inspect one source and return bounded evidence",
                    "tools": ["read_file"],
                },
                _context("read_file"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertTrue(result["retryable"])
        self.assertEqual("model_output_limit", result["failure_class"])
        self.assertEqual(
            ["read_file"],
            result["dispatch_receipt_audit"]["read_only_tool_names"],
        )
        self.assertEqual(
            [],
            result["dispatch_receipt_audit"]["mutating_tool_names"],
        )
        persist_result.assert_not_called()

    async def test_mutation_receipt_overrides_runtime_output_contract_retry(self):
        async def fake_run_stream(model_id, messages, tools, **kwargs):
            call_id = "write-before-contract-failure"
            yield _started("write_file", call_id)
            yield _dispatch_started("write_file", call_id)
            yield _completed("write_file", call_id)
            error = "The delegated typed output repair remained invalid."
            yield {
                "type": "agent_event",
                "event_type": "run.failed",
                "payload": {
                    "error": error,
                    "finish_reason": (
                        "delegated_output_contract_repair_continuation_invalid"
                    ),
                    "failure_class": "agent_contract_noncompliance",
                    # Even an optimistic inner hint cannot override the
                    # parent-owned mutation receipt.
                    "retryable": True,
                },
            }
            yield {"type": "error", "msg": error}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch("tools.delegation.persist_result_for_history") as persist_result,
        ):
            result = await _run_child(
                {
                    "goal": "write one artifact and return its typed receipt",
                    "tools": ["write_file"],
                    "required_result_fields": ["artifact_path"],
                },
                _context("write_file"),
                0,
            )

        self.assertEqual("error", result["status"])
        self.assertFalse(result["retryable"])
        self.assertEqual(
            "agent_contract_noncompliance",
            result["failure_class"],
        )
        self.assertEqual(
            ["write_file"],
            result["dispatch_receipt_audit"]["mutating_tool_names"],
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
