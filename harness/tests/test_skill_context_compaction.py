import hashlib
import json
import unittest

from context.compressor import ContextCompressor


def _tool_call(call_id, name, arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }],
    }


def _tool_result(call_id, content):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content if isinstance(content, str) else json.dumps(
            content,
            ensure_ascii=False,
        ),
    }


def _main_skill_pair(call_id, skill_name, body, *, raw_document=None):
    raw = raw_document or (
        f"---\nname: {skill_name}\ndescription: test skill\n---\n{body}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    result = {
        "success": True,
        "name": skill_name,
        "content": body,
        "skill_md_sha256": digest,
        "skill_md_chars": len(raw),
    }
    return (
        _tool_call(call_id, "skill_view", {"name": skill_name}),
        _tool_result(call_id, result),
        digest,
    )


def _paged_skill_pair(
    call_id,
    skill_name,
    full_document,
    *,
    offset,
    limit,
    initial=False,
):
    content = full_document[offset:offset + limit]
    total = len(full_document)
    next_offset = offset + len(content)
    has_more = next_offset < total
    digest = hashlib.sha256(full_document.encode("utf-8")).hexdigest()
    arguments = {"name": skill_name}
    if not initial:
        arguments.update({
            "file_path": "SKILL.md",
            "offset": offset,
            "limit": limit,
        })
    result = {
        "success": True,
        "name": skill_name,
        "file": "SKILL.md",
        "content": content,
        "sha256": digest,
        "offset": offset,
        "limit": limit,
        "returned_chars": len(content),
        "total_chars": total,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
        "truncated": has_more,
        "pagination": {
            "unit": "unicode_codepoints",
            "offset": offset,
            "limit": limit,
            "returned_chars": len(content),
            "total_chars": total,
            "next_offset": next_offset if has_more else None,
            "has_more": has_more,
        },
    }
    if initial:
        result.update({
            "skill_md_sha256": digest,
            "skill_md_chars": total,
            "main_document_paged": True,
        })
    return _tool_call(call_id, "skill_view", arguments), _tool_result(
        call_id, result
    )


def _conversation(*middle):
    return [
        {"role": "system", "content": "system policy"},
        {"role": "user", "content": "perform the active task"},
        *middle,
        {"role": "assistant", "content": "intermediate observation"},
        {"role": "user", "content": "keep going"},
        {"role": "assistant", "content": "latest partial answer"},
        {"role": "user", "content": "finish the task"},
    ]


def _compressor(*, context_length=131_072):
    compressor = ContextCompressor(
        context_length=context_length,
        protect_first_n=0,
        protect_last_n=2,
    )
    # Force a small, deterministic protected tail in these unit fixtures.
    compressor.tail_token_budget = 1
    return compressor


def _native_call_ids(messages):
    call_ids = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            call_ids.add(tool_call.get("id") or tool_call.get("call_id"))
    return {call_id for call_id in call_ids if call_id}


def _native_result_ids(messages):
    return {
        message.get("tool_call_id")
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }


def _skill_body_occurrences(messages, body):
    occurrences = 0
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            result = json.loads(message.get("content") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(result, dict) and result.get("content") == body:
            occurrences += 1
    return occurrences


class SkillContextCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_skill_pair_survives_without_entering_summary(self):
        skill_body = "CANONICAL-SKILL-INSTRUCTION\n" * 80
        skill_call, skill_result, _ = _main_skill_pair(
            "skill-main", "generic-planner", skill_body
        )
        ordinary_body = "ordinary-large-tool-output-" * 100
        ordinary_call = _tool_call(
            "ordinary-read", "read_file", {"filepath": "notes.txt"}
        )
        ordinary_result = _tool_result("ordinary-read", ordinary_body)
        reference_body = "supporting-reference-body-" * 100
        reference_call = _tool_call(
            "skill-reference",
            "skill_view",
            {
                "name": "generic-planner",
                "file_path": "references/guide.md",
            },
        )
        reference_result = _tool_result("skill-reference", {
            "success": True,
            "name": "generic-planner",
            "file": "references/guide.md",
            "content": reference_body,
            "sha256": hashlib.sha256(
                reference_body.encode("utf-8")
            ).hexdigest(),
        })
        messages = _conversation(
            skill_call,
            skill_result,
            ordinary_call,
            ordinary_result,
            reference_call,
            reference_result,
        )
        compressor = _compressor()
        summarized = []

        async def capture_summary(turns, focus_topic=None):
            summarized.extend(turns)
            return compressor._with_summary_prefix("bounded summary")

        compressor._generate_summary = capture_summary
        compressed = await compressor.compress(messages, force=True)

        summarized_json = json.dumps(summarized, ensure_ascii=False)
        compressed_json = json.dumps(compressed, ensure_ascii=False)
        self.assertNotIn(skill_body, summarized_json)
        self.assertNotIn(ordinary_body, summarized_json)
        self.assertNotIn(reference_body, summarized_json)
        self.assertNotIn(ordinary_body, compressed_json)
        self.assertNotIn(reference_body, compressed_json)
        self.assertEqual(1, _skill_body_occurrences(compressed, skill_body))
        self.assertEqual(1, compressor._last_protected_skill_receipt_count)

        skill_index = next(
            index
            for index, message in enumerate(compressed)
            if message.get("role") == "assistant"
            and any(
                call.get("id") == "skill-main"
                for call in message.get("tool_calls") or []
            )
        )
        self.assertEqual("tool", compressed[skill_index + 1]["role"])
        self.assertEqual(
            "skill-main", compressed[skill_index + 1]["tool_call_id"]
        )
        self.assertEqual(skill_result["content"], compressed[skill_index + 1]["content"])
        for message in compressed:
            if message.get("role") != "tool":
                self.assertNotIn(skill_body, json.dumps(message, ensure_ascii=False))
        self.assertEqual(_native_call_ids(compressed), _native_result_ids(compressed))

    async def test_latest_digest_and_latest_contiguous_pages_are_deduplicated(self):
        old_call, old_result, old_digest = _main_skill_pair(
            "old-main", "paged-skill", "OLD-INSTRUCTIONS" * 20
        )
        new_document = "ABCDEFGHIJ"
        page_zero_old = _paged_skill_pair(
            "new-page-zero-old",
            "paged-skill",
            new_document,
            offset=0,
            limit=5,
            initial=True,
        )
        page_one = _paged_skill_pair(
            "new-page-one",
            "paged-skill",
            new_document,
            offset=5,
            limit=5,
        )
        page_zero_latest = _paged_skill_pair(
            "new-page-zero-latest",
            "paged-skill",
            new_document,
            offset=0,
            limit=5,
            initial=True,
        )
        messages = _conversation(
            old_call,
            old_result,
            *page_zero_old,
            *page_one,
            *page_zero_latest,
        )
        compressor = _compressor()

        async def summary(turns, focus_topic=None):
            return compressor._with_summary_prefix("paged summary")

        compressor._generate_summary = summary
        compressed = await compressor.compress(messages, force=True)

        calls = [
            call
            for message in compressed
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if call.get("function", {}).get("name") == "skill_view"
        ]
        self.assertEqual(
            ["new-page-zero-latest", "new-page-one"],
            [call.get("id") for call in calls],
        )
        self.assertEqual(2, compressor._last_protected_skill_receipt_count)
        serialized = json.dumps(compressed, ensure_ascii=False)
        self.assertNotIn("old-main", serialized)
        self.assertNotIn("new-page-zero-old", serialized)
        self.assertNotIn(old_digest, serialized)
        self.assertEqual(_native_call_ids(compressed), _native_result_ids(compressed))

    async def test_invalid_failed_pointer_and_missing_digest_views_are_not_protected(self):
        pseudo_body = "PSEUDO-SKILL-BODY-" * 100
        valid_call, valid_result, _ = _main_skill_pair(
            "pointer-view", "unsafe-skill", pseudo_body
        )
        persisted_pointer = (
            valid_result["content"]
            + "\n[Full result persisted to sandbox: results/skill_view.txt]"
        )
        invalid_pairs = [
            (
                _tool_call("non-json", "skill_view", {"name": "unsafe-skill"}),
                _tool_result("non-json", pseudo_body),
            ),
            (
                _tool_call("failed-view", "skill_view", {"name": "unsafe-skill"}),
                _tool_result("failed-view", {
                    "success": False,
                    "name": "unsafe-skill",
                    "content": pseudo_body,
                    "skill_md_sha256": "a" * 64,
                    "skill_md_chars": len(pseudo_body),
                }),
            ),
            (valid_call, _tool_result("pointer-view", persisted_pointer)),
            (
                _tool_call("missing-digest", "skill_view", {"name": "unsafe-skill"}),
                _tool_result("missing-digest", {
                    "success": True,
                    "name": "unsafe-skill",
                    "content": pseudo_body,
                    "skill_md_chars": len(pseudo_body),
                }),
            ),
        ]
        messages = _conversation(
            *(message for pair in invalid_pairs for message in pair)
        )
        compressor = _compressor()

        async def summary(turns, focus_topic=None):
            return compressor._with_summary_prefix("invalid views summarized")

        compressor._generate_summary = summary
        compressed = await compressor.compress(messages, force=True)

        self.assertEqual(0, compressor._last_protected_skill_receipt_count)
        serialized = json.dumps(compressed, ensure_ascii=False)
        self.assertNotIn(pseudo_body, serialized)
        for call_id in (
            "non-json", "failed-view", "pointer-view", "missing-digest"
        ):
            self.assertNotIn(call_id, _native_call_ids(compressed))
            self.assertNotIn(call_id, _native_result_ids(compressed))
        self.assertEqual(_native_call_ids(compressed), _native_result_ids(compressed))

    async def test_protected_skill_budget_overflow_aborts_without_partial_rewrite(self):
        body = "B" * 20_000
        skill_call, skill_result, _ = _main_skill_pair(
            "oversized-skill", "large-generic-skill", body
        )
        messages = _conversation(skill_call, skill_result)
        compressor = _compressor(context_length=8_000)
        summary_called = False

        async def summary(turns, focus_topic=None):
            nonlocal summary_called
            summary_called = True
            return compressor._with_summary_prefix("must not run")

        compressor._generate_summary = summary
        compressed = await compressor.compress(messages, force=True)

        self.assertIs(messages, compressed)
        self.assertFalse(summary_called)
        self.assertTrue(compressor._last_compress_aborted)
        self.assertIn(
            "protected_skill_context_budget_exceeded",
            compressor._last_summary_error,
        )
        self.assertGreater(
            compressor._last_protected_skill_tokens,
            compressor._protected_skill_token_budget(),
        )
        self.assertEqual(0, compressor.compression_count)
        self.assertTrue(compressor.get_status()["last_compress_aborted"])
        self.assertTrue(
            compressor.get_status()["protected_skill_budget_exceeded"]
        )

    async def test_summary_failure_and_repeated_compaction_keep_exact_native_pair(self):
        body = "RETAIN-ON-SUMMARY-FAILURE\n" * 60
        skill_call, skill_result, _ = _main_skill_pair(
            "durable-skill", "durable-generic-skill", body
        )
        compressor = _compressor()

        async def failed_summary(turns, focus_topic=None):
            return None

        compressor._generate_summary = failed_summary
        first = await compressor.compress(
            _conversation(skill_call, skill_result),
            force=True,
        )
        self.assertTrue(compressor._last_summary_fallback_used)
        self.assertEqual(1, _skill_body_occurrences(first, body))
        self.assertEqual(_native_call_ids(first), _native_result_ids(first))

        async def later_summary(turns, focus_topic=None):
            return compressor._with_summary_prefix("second summary")

        compressor._generate_summary = later_summary
        second = await compressor.compress(
            first + [
                {"role": "assistant", "content": "more work"},
                {"role": "user", "content": "continue"},
                {"role": "assistant", "content": "nearly done"},
                {"role": "user", "content": "finish now"},
            ],
            force=True,
        )
        self.assertEqual(1, _skill_body_occurrences(second, body))
        self.assertEqual(1, compressor._last_protected_skill_receipt_count)
        self.assertEqual(_native_call_ids(second), _native_result_ids(second))


if __name__ == "__main__":
    unittest.main()
