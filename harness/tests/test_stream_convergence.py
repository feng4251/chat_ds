from contextlib import asynccontextmanager
import unittest

from stream_convergence import StreamConvergenceAbort, StreamConvergenceGuard


class StreamConvergenceGuardTests(unittest.TestCase):
    def test_fragmented_malformed_raw_protocol_cycle_is_bounded(self):
        guard = StreamConvergenceGuard(max_tokens=8192)
        block = "<tool_call>execute_code`\ncode={\"b\":1}\n`\n"
        with self.assertRaises(StreamConvergenceAbort) as caught:
            for char in block * 4:
                guard.observe_provider_fragment(content_chars=1)
                guard.observe_content(char)
        self.assertEqual(
            caught.exception.code,
            "raw_pseudo_tool_protocol_cycle",
        )
        self.assertGreaterEqual(
            caught.exception.metrics["raw_protocol_marker_count"], 3
        )

    def test_markdown_protocol_examples_do_not_trigger(self):
        guard = StreamConvergenceGuard(max_tokens=8192)
        example = (
            "```text\n<tool_call>execute_code`\ncode={}\n`\n"
            "<tool_call>execute_code`\ncode={}\n`\n"
            "<tool_call>execute_code`\ncode={}\n`\n```\n"
        )
        for char in example:
            guard.observe_provider_fragment(content_chars=1)
            guard.observe_content(char)

    def test_long_exact_reasoning_cycle_aborts(self):
        guard = StreamConvergenceGuard(max_tokens=8192)
        cycle = (
            "Plan the bounded evidence query, inspect its result, and make "
            "one native capability call before returning a typed status. "
        ) * 12
        with self.assertRaises(StreamConvergenceAbort) as caught:
            for _ in range(12):
                guard.observe_provider_fragment(reasoning_chars=len(cycle))
                guard.observe_reasoning(
                    cycle,
                    visible_content_seen=False,
                    structured_tool_fragment_seen=False,
                )
        self.assertEqual(caught.exception.code, "reasoning_cycle_detected")
        self.assertGreaterEqual(
            caught.exception.metrics["repeated_cycle_windows"], 64
        )

    def test_long_novel_reasoning_is_not_a_cycle(self):
        guard = StreamConvergenceGuard(max_tokens=8192)
        for index in range(900):
            value = (
                f"Unique bounded observation {index:04d} has digest-shaped "
                f"evidence marker {index * 104729:09d}. "
            )
            guard.observe_provider_fragment(reasoning_chars=len(value))
            guard.observe_reasoning(
                value,
                visible_content_seen=False,
                structured_tool_fragment_seen=False,
            )

    def test_absolute_reasoning_and_fragment_limits_fail_closed(self):
        guard = StreamConvergenceGuard(max_tokens=1)
        with self.assertRaises(StreamConvergenceAbort) as chars:
            guard.observe_provider_fragment(
                reasoning_chars=guard.reasoning_char_limit + 1
            )
        self.assertEqual(chars.exception.code, "reasoning_char_limit_exceeded")

        guard = StreamConvergenceGuard(max_tokens=1)
        guard.provider_fragments = guard.provider_fragment_limit
        with self.assertRaises(StreamConvergenceAbort) as fragments:
            guard.observe_provider_fragment()
        self.assertEqual(
            fragments.exception.code,
            "provider_fragment_limit_exceeded",
        )

    def test_semantic_checks_can_be_disabled_for_primary_streams(self):
        guard = StreamConvergenceGuard(
            max_tokens=8192,
            semantic_checks_enabled=False,
        )
        raw = "<tool_call>example`\ncode={}\n`\n" * 8
        cycle = "repeat the same explanatory paragraph " * 1000
        guard.observe_provider_fragment(
            content_chars=len(raw), reasoning_chars=len(cycle)
        )
        guard.observe_content(raw)
        guard.observe_reasoning(
            cycle,
            visible_content_seen=False,
            structured_tool_fragment_seen=False,
        )


class StreamConvergenceAbortPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_abort_reason_survives_async_generator_context_exit(self):
        @asynccontextmanager
        async def httpx_style_stream():
            yield object()

        original = StreamConvergenceAbort(
            code="reasoning_cycle_detected",
            metrics={"normalized_reasoning_chars": 12_288},
        )

        with self.assertRaises(StreamConvergenceAbort) as caught:
            async with httpx_style_stream():
                raise original

        self.assertIs(caught.exception, original)
        self.assertEqual(caught.exception.code, "reasoning_cycle_detected")
        self.assertEqual(str(caught.exception), "reasoning_cycle_detected")
        self.assertEqual(
            caught.exception.metrics,
            {"normalized_reasoning_chars": 12_288},
        )


if __name__ == "__main__":
    unittest.main()
