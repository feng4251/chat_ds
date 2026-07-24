import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from unittest.mock import ANY, AsyncMock, Mock, patch

from agent_loop import (
    HarnessRunState,
    _apply_intent_selections_to_plan,
    _bounded_session_skill_relevance_selection,
    _bootstrap_required_child_capability_tools,
    _clamp_max_tokens_for_context,
    _bounded_skill_execution_exposure,
    _build_skill_execution_plan,
    _complex_report_context,
    _compiled_skill_inspection_target,
    _continuation_unique_suffix,
    _declared_capability_skill_names,
    _declared_child_tools,
    _declared_result_field_names,
    _declared_result_field_schema,
    _delegated_child_iteration_limit,
    _deterministic_complex_skill_selection,
    _direct_chat_tool_exposure,
    _estimate_payload_tokens,
    _explicit_skill_workflow_request,
    _explicit_required_child_capability_tools,
    _explicit_declared_intent_selections,
    _is_repeated_length_response,
    _looks_like_complex_artifact_request,
    _looks_like_file_artifact_request,
    _preferred_initial_required_capability_tools,
    _profile_bound_child_runner_kwargs,
    _retry_max_tokens_from_context_overflow,
    _should_recover_tool_failure,
    _skill_documentation_tool_exposure,
    _session_skill_relevance_inspection_exposure,
    _skill_workflow_activation_for_request,
    _stream_retry_is_safe,
    _workflow_gate_call_error,
    _workflow_gate_tool_policy,
    run_stream,
)
from tools.context import ToolContext
from tools.registry import delegated_resource_boundary_error
from tools.skill_runtime_profile import (
    compile_skill_runtime_profile_manifest,
)
from context.compressor import ContextCompressor, _estimate_messages_tokens
from config import (
    DEFAULT_AGENT_MODEL_ID,
    PROVIDERS,
    canonical_provider_id,
)
from prompt.builder import IMAGE_SKILL_MCP_GUIDANCE, SESSION_SKILL_USAGE_GUIDANCE
from tools.web_extract import web_extract


class MultimodalTokenSafetyTests(unittest.TestCase):
    def test_data_url_transport_bytes_are_not_counted_as_text_tokens(self):
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Convert this image to Markdown."},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64," + "A" * 1_005_000,
                        "detail": "auto",
                    },
                },
            ],
        }]

        estimated = _estimate_payload_tokens(messages, [])
        effective, budget = _clamp_max_tokens_for_context(8192, 262_144, estimated)

        self.assertLess(estimated, 5_000)
        self.assertEqual(
            _estimate_messages_tokens(messages),
            _estimate_payload_tokens(messages, []) - len(messages) * 4,
        )
        self.assertEqual(effective, 8192)
        self.assertGreater(budget["available_output_tokens"], 200_000)

    def test_large_plain_text_is_still_counted_normally(self):
        estimated = _estimate_payload_tokens([
            {"role": "user", "content": "A" * 1_000_000}
        ], [])

        self.assertGreater(estimated, 200_000)

    def test_cjk_text_uses_conservative_non_ascii_token_floor(self):
        text = "临床试验设计与证据质量验证" * 1_000
        estimated = _estimate_payload_tokens([
            {"role": "user", "content": text}
        ], [])

        self.assertGreaterEqual(estimated, len(text))

    def test_anthropic_base64_image_is_bounded_but_caption_is_counted(self):
        base = {
            "role": "user",
            "content": [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "A" * 1_000_000,
                },
                "caption": "short caption",
            }],
        }
        longer = {
            "role": "user",
            "content": [{**base["content"][0], "caption": "x" * 4_000}],
        }

        bounded = _estimate_payload_tokens([base], [])
        self.assertLess(bounded, 5_000)
        self.assertGreater(_estimate_payload_tokens([longer], []), bounded + 500)

    def test_context_summary_omits_image_transport_but_keeps_text_and_metadata(self):
        base64_payload = "BASE64_SENTINEL_" + "A" * 1_000_000
        serialized = ContextCompressor()._serialize_for_summary([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Keep this ordinary instruction."},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64_payload,
                        "detail": "high",
                    },
                    "caption": "scanned consent form",
                    "page": 2,
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64_payload,
                    },
                    "alt": "secondary scan",
                },
                {
                    "type": "input_image",
                    "image_url": "https://example.invalid/private-scan.png",
                    "caption": "remote scan",
                },
            ],
        }])

        self.assertNotIn("BASE64_SENTINEL_", serialized)
        self.assertNotIn("data:image", serialized)
        self.assertNotIn("example.invalid", serialized)
        self.assertNotIn('"url"', serialized)
        self.assertNotIn('"data"', serialized)
        self.assertLess(len(serialized), 5_000)
        self.assertIn("Keep this ordinary instruction.", serialized)
        self.assertIn("scanned consent form", serialized)
        self.assertIn("secondary scan", serialized)
        self.assertIn("remote scan", serialized)
        self.assertIn('"detail": "high"', serialized)
        self.assertIn('"page": 2', serialized)

    def test_image_detail_changes_bounded_vision_estimate(self):
        def estimate(detail: str) -> int:
            return _estimate_payload_tokens([{
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": "https://example.invalid/i.png", "detail": detail},
                }],
            }], [])

        self.assertLess(estimate("low"), estimate("high"))

    def test_context_clamp_never_invents_a_512_token_budget(self):
        context_length = 20_000
        safety_margin = 2_000

        effective_511, _ = _clamp_max_tokens_for_context(
            8192, context_length, context_length - safety_margin - 511
        )
        effective_512, _ = _clamp_max_tokens_for_context(
            8192, context_length, context_length - safety_margin - 512
        )
        effective_8192, _ = _clamp_max_tokens_for_context(
            8192, context_length, context_length - safety_margin - 8192
        )
        effective_small, _ = _clamp_max_tokens_for_context(
            256, context_length, context_length - safety_margin - 300
        )

        self.assertEqual(effective_511, 0)
        self.assertEqual(effective_512, 512)
        self.assertEqual(effective_8192, 8192)
        self.assertEqual(effective_small, 256)

    def test_restarted_truncated_response_is_detected(self):
        previous = "# Attachment 2\n" + ("recognized line\n" * 100)
        restarted = "# Attachment 2\n" + ("recognized line\n" * 95) + "tail"
        continuation = "new continuation paragraph\n" * 100

        self.assertTrue(_is_repeated_length_response(previous, restarted))
        self.assertFalse(_is_repeated_length_response(previous, continuation))
        self.assertEqual(
            _continuation_unique_suffix("alpha " + "x" * 40, "x" * 40 + " omega"),
            " omega",
        )
        previous_long = "P" * 5_000
        self.assertEqual(
            _continuation_unique_suffix(previous_long, previous_long + "\nDONE"),
            "\nDONE",
        )

    def test_provider_overflow_uses_requested_floor_and_preserves_budget(self):
        error = (
            "maximum context length is 10000 tokens; requested 256 output tokens; "
            "prompt contains at least 8800 input tokens"
        )
        adjusted, budget = _retry_max_tokens_from_context_overflow(error, 256)

        self.assertIsNone(adjusted)
        self.assertEqual(budget["minimum_useful_output_tokens"], 256)
        self.assertEqual(budget["available_output_tokens"], 176)

        reducible = (
            "maximum context length is 10000 tokens; requested 8192 output tokens; "
            "prompt contains at least 8000 input tokens"
        )
        adjusted, budget = _retry_max_tokens_from_context_overflow(reducible, 8192)
        self.assertEqual(adjusted, 976)
        self.assertEqual(budget["effective_max_tokens"], 976)

    def test_vllm_max_total_tokens_error_adapts_stale_model_capacity(self):
        limit_fragments = (
            "max_model_len=max_total_tokens=250368",
            "max_model_len=250368",
            "max_total_tokens=250368",
        )
        for limit_fragment in limit_fragments:
            with self.subTest(limit_fragment=limit_fragment):
                error = (
                    "max_tokens=262144 cannot be greater than "
                    f"{limit_fragment}. Please request fewer output tokens. "
                    "(parameter=max_tokens, value=262144)"
                )

                adjusted, budget = _retry_max_tokens_from_context_overflow(
                    error,
                    262_144,
                    518,
                )

                self.assertEqual(233_466, adjusted)
                self.assertEqual(250_368, budget["context_length"])
                self.assertEqual(518, budget["prompt_tokens"])
                self.assertEqual("vllm_max_total_tokens", budget["source"])

    def test_stream_retry_is_forbidden_after_visible_or_reasoning_delta(self):
        self.assertTrue(_stream_retry_is_safe("", ""))
        self.assertFalse(_stream_retry_is_safe("partial", ""))
        self.assertFalse(_stream_retry_is_safe("", "reasoning"))

    def test_markdown_chat_format_is_not_durable_artifact_intent(self):
        self.assertFalse(_looks_like_file_artifact_request("将这张图的文本转为markdown输出"))
        self.assertFalse(_looks_like_file_artifact_request("Write the answer as a report in chat"))
        self.assertTrue(_looks_like_file_artifact_request("保存为 result.md"))
        self.assertTrue(_looks_like_file_artifact_request("write to file output/report.pdf"))
        self.assertTrue(_looks_like_file_artifact_request("在 workspace 中生成结果"))
        self.assertFalse(_looks_like_file_artifact_request("读取 README.md 并解释"))
        self.assertFalse(_looks_like_file_artifact_request("inspect workspace and summarize"))
        self.assertFalse(_looks_like_file_artifact_request("不要保存文件，直接输出 report.md 的内容"))
        self.assertFalse(_looks_like_file_artifact_request("不要创建文件，只在聊天中回答"))
        self.assertFalse(_looks_like_file_artifact_request("请勿新建或写入文件，直接给建议"))
        self.assertFalse(_looks_like_file_artifact_request("不需要导出文件；只回答结论"))
        self.assertFalse(_looks_like_file_artifact_request("Do not create a file; answer inline"))
        self.assertFalse(_looks_like_file_artifact_request("Answer without creating a file"))
        self.assertFalse(_looks_like_file_artifact_request("Generate a summary of README.md"))
        self.assertFalse(_looks_like_file_artifact_request("Write a critique of input.json"))
        self.assertFalse(_looks_like_file_artifact_request("Create a report about the workspace"))
        self.assertFalse(_looks_like_file_artifact_request("请生成 README.md 的摘要"))
        self.assertTrue(_looks_like_file_artifact_request("编辑 README.md 修复链接"))
        self.assertTrue(_looks_like_file_artifact_request("Save result.md and also paste it in chat"))
        self.assertTrue(_looks_like_file_artifact_request(
            "不要创建草稿文件；保存最终结果为 result.md"
        ))
        self.assertTrue(_looks_like_file_artifact_request(
            "Don't edit old.md, but create new.md"
        ))


class WorkflowGateToolPolicyTests(unittest.TestCase):
    def setUp(self):
        self.available = {
            "skill_view", "delegate_task", "read_file", "write_file",
            "merge_files", "clarify", "web_search",
        }

    def test_intent_gate_exposes_only_delegate_and_requires_toolless_child(self):
        policy = _workflow_gate_tool_policy(
            "delegate intent classification for session skill 'generic'",
            self.available,
        )

        self.assertEqual(policy["tools"], ["delegate_task"])
        valid = {
            "goal": "classify",
            "skill_name": "generic",
            "step_type": "intent_classification",
            "tools": [],
            "max_iterations": 2,
        }
        self.assertEqual(
            _workflow_gate_call_error(policy, "delegate_task", valid, prior_call_count=0),
            "",
        )
        self.assertIn(
            "permits only",
            _workflow_gate_call_error(policy, "write_file", {}, prior_call_count=0),
        )
        unsafe = {**valid, "tools": ["skill_view"]}
        self.assertIn(
            "explicit empty",
            _workflow_gate_call_error(policy, "delegate_task", unsafe, prior_call_count=0),
        )

    def test_intent_gate_rejects_pre_resolved_selections_and_skill_files(self):
        policy = _workflow_gate_tool_policy(
            "delegate intent classification for session skill 'generic'",
            self.available,
        )
        invalid = {
            "goal": "resolve explicit intent",
            "skill_name": "generic",
            "step_type": "intent_classification",
            "tools": [],
            "max_iterations": 2,
            "deterministic_intent_selections": {
                "task_kind": "comprehensive",
                "phase": "all",
            },
            "required_skill_files": ["resources/a.md", "resources/b.md"],
        }

        self.assertIn(
            "may not preload",
            _workflow_gate_call_error(
                policy, "delegate_task", invalid, prior_call_count=0,
            ),
        )

    def test_worker_gate_requires_declared_step_and_skill(self):
        policy = _workflow_gate_tool_policy(
            "delegate workflow stage 'research' for session skill 'generic' (parallel workers: a)",
            self.available,
        )

        valid = {
            "tasks": [{
                "goal": "run worker",
                "skill_name": "generic",
                "step_type": "worker",
                "tools": ["read_file"],
            }]
        }
        self.assertEqual(
            _workflow_gate_call_error(policy, "delegate_task", valid, prior_call_count=0),
            "",
        )
        wrong = {"tasks": [{**valid["tasks"][0], "step_type": "artifact_synthesis"}]}
        self.assertIn(
            "step_type=worker",
            _workflow_gate_call_error(policy, "delegate_task", wrong, prior_call_count=0),
        )

        policy["expected_step_ids"] = ["research-a"]
        policy["expected_worker_files"] = {"research-a": "workers/research-a.yaml"}
        exact = {
            "tasks": [{
                "goal": "run worker",
                "skill_name": "generic",
                "step_type": "worker",
                "step_id": "research-a",
                "worker_id": "research-a",
                "worker_file": "workers/research-a.yaml",
                "tools": ["skill_view", "read_file"],
            }]
        }
        self.assertEqual(
            _workflow_gate_call_error(policy, "delegate_task", exact, prior_call_count=0),
            "",
        )
        exact["tasks"][0]["worker_file"] = "workers/wrong.yaml"
        self.assertIn(
            "exact contract file",
            _workflow_gate_call_error(policy, "delegate_task", exact, prior_call_count=0),
        )

    def test_exact_controller_only_worker_and_aggregation_allow_zero_model_tools(self):
        binding = {
            "candidate_id": "delegate-controller",
            "kind": "native_tool",
            "tool_name": "delegate_task",
            "tool_names": ["delegate_task"],
        }
        digest = hashlib.sha256(json.dumps(
            [binding],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        source_binding = {
            "resource_path": "SKILL.md",
            "sha256": "a" * 64,
        }

        worker_policy = _workflow_gate_tool_policy(
            "delegate workflow stage 'reasoning' for session skill 'generic' "
            "(parallel workers: reason)",
            self.available,
        )
        worker_policy.update({
            "expected_step_ids": ["reason"],
            "expected_worker_files": {
                "reason": "workers/reason.md",
            },
            "expected_capability_bindings": {
                "reason": [binding],
            },
            "expected_capability_bindings_sha256": {
                "reason": digest,
            },
            "expected_instruction_source_bindings": {
                "reason": [source_binding],
            },
            "expected_required_result_paths": {
                "reason": [],
            },
            "expected_skill_files_to_inspect": {
                "reason": ["SKILL.md"],
            },
        })
        worker = {
            "goal": "reason from the exact instruction",
            "skill_name": "generic",
            "step_type": "worker",
            "step_id": "reason",
            "worker_id": "reason",
            "worker_file": "workers/reason.md",
            "tools": [],
            "required_skill_files_to_inspect": ["SKILL.md"],
            "required_instruction_source_bindings": [source_binding],
            "capability_bindings": [binding],
            "capability_bindings_sha256": digest,
        }
        self.assertEqual(
            "",
            _workflow_gate_call_error(
                worker_policy,
                "delegate_task",
                worker,
                prior_call_count=0,
            ),
        )

        aggregation_policy = _workflow_gate_tool_policy(
            "delegate aggregation step 'synthesize' for session skill 'generic'",
            self.available,
        )
        aggregation_policy.update({
            "expected_step_ids": ["synthesize"],
            "expected_capability_bindings": {
                "synthesize": [binding],
            },
            "expected_capability_bindings_sha256": {
                "synthesize": digest,
            },
            "expected_instruction_source_bindings": {
                "synthesize": [source_binding],
            },
            "expected_required_result_paths": {
                "synthesize": ["results/reason.txt"],
            },
            "expected_skill_files_to_inspect": {
                "synthesize": ["SKILL.md"],
            },
        })
        aggregation = {
            "goal": "synthesize the preloaded result",
            "skill_name": "generic",
            "step_type": "aggregation",
            "step_id": "synthesize",
            "tools": [],
            "required_result_paths": ["results/reason.txt"],
            "required_skill_files_to_inspect": ["SKILL.md"],
            "required_instruction_source_bindings": [source_binding],
            "capability_bindings": [binding],
            "capability_bindings_sha256": digest,
        }
        self.assertEqual(
            "",
            _workflow_gate_call_error(
                aggregation_policy,
                "delegate_task",
                aggregation,
                prior_call_count=0,
            ),
        )

        for invalid, expected_error in (
            (
                {**worker, "capability_bindings_sha256": "0" * 64},
                "explicit empty tools allowlist",
            ),
            (
                {
                    key: value
                    for key, value in worker.items()
                    if key != "worker_file"
                },
                "exact contract file",
            ),
            (
                {
                    "goal": "ad hoc empty delegate",
                    "skill_name": "generic",
                    "step_type": "worker",
                    "step_id": "reason",
                    "worker_id": "reason",
                    "worker_file": "workers/reason.md",
                    "tools": [],
                },
                "explicit empty tools allowlist",
            ),
        ):
            with self.subTest(invalid=invalid):
                self.assertIn(
                    expected_error,
                    _workflow_gate_call_error(
                        worker_policy,
                        "delegate_task",
                        invalid,
                        prior_call_count=0,
                    ),
                )

    def test_worker_gate_rejects_duplicate_or_missing_extra_step_ids(self):
        policy = _workflow_gate_tool_policy(
            "delegate workflow stage 'research' for session skill 'generic' (parallel workers: a, b)",
            self.available,
        )
        policy["expected_step_ids"] = ["a", "b"]

        duplicate = {"tasks": [
            {"goal": "a1", "skill_name": "generic", "step_type": "worker", "step_id": "a", "tools": ["read_file"]},
            {"goal": "a2", "skill_name": "generic", "step_type": "worker", "step_id": "a", "tools": ["read_file"]},
            {"goal": "b", "skill_name": "generic", "step_type": "worker", "step_id": "b", "tools": ["read_file"]},
        ]}
        missing_extra = {"tasks": [
            {"goal": "a", "skill_name": "generic", "step_type": "worker", "step_id": "a", "tools": ["read_file"]},
            {"goal": "b", "skill_name": "generic", "step_type": "worker", "step_id": "b", "tools": ["read_file"]},
            {"goal": "extra", "skill_name": "generic", "step_type": "worker", "tools": ["read_file"]},
        ]}

        self.assertIn("exact step IDs", _workflow_gate_call_error(
            policy, "delegate_task", duplicate, prior_call_count=0,
        ))
        self.assertIn("exact step IDs", _workflow_gate_call_error(
            policy, "delegate_task", missing_extra, prior_call_count=0,
        ))

    def test_inspection_merge_and_blocked_policies_are_fail_closed(self):
        inspection = _workflow_gate_tool_policy(
            "inspect explicit workflow resources for session skill 'generic'",
            self.available,
        )
        merge = _workflow_gate_tool_policy(
            "create the declared merged final report artifact for session skill 'generic'",
            self.available,
        )
        blocked = _workflow_gate_tool_policy(
            "resolve blocked workflow dependencies for session skill 'generic' stage 'review'",
            self.available,
        )

        self.assertEqual(inspection["tools"], ["skill_view"])
        self.assertEqual(merge["tools"], ["merge_files"])
        self.assertEqual(blocked["tools"], [])

    def test_available_session_skill_alone_does_not_upgrade_simple_chat(self):
        state = HarnessRunState(
            user_id="u",
            session_id="s",
            available_tools=set(),
            original_user_text="What does this image say?",
        )
        state.session_skill_names.add("unrelated-complex-skill")

        self.assertFalse(_complex_report_context(state, state.original_user_text))

        state.skill_execution_plans["activated"] = {"requires_full_output": True}
        self.assertTrue(_complex_report_context(state, state.original_user_text))

    def test_contract_capabilities_resolve_to_closed_child_tool_aliases(self):
        tools = _declared_child_tools(
            {
                "skill_view", "read_file", "search_files", "web_search",
                "run_skill_python", "write_file",
            },
            [
                {"name": "guideline_search", "source": "WebSearch"},
                {"name": "catalog", "source": "skill:catalog-database"},
            ],
            runnable_capability_skills={"catalog-database"},
        )

        self.assertEqual(
            tools,
            [
                "skill_view", "read_file", "search_files", "web_search",
                "run_skill_python",
            ],
        )
        self.assertNotIn("write_file", tools)
        self.assertEqual(
            _explicit_required_child_capability_tools(
                [
                    {"name": "guideline_search", "source": "WebSearch"},
                    {
                        "name": "catalog",
                        "source": "skill:catalog-database",
                    },
                ],
                tools,
            ),
            [],
        )

    def test_clinical_worker_tools_are_available_without_becoming_mandatory(self):
        """A shared-evidence clinical worker must not repeat bootstrap queries."""

        declaration = {
            "tools": [
                {
                    "name": "fda_search",
                    "source": "skill:fda-database",
                },
                {
                    "name": "pubmed_search",
                    "source": "skill:pubmed-database",
                },
                {
                    "name": "web_search",
                    "source": "WebSearch",
                },
            ],
        }
        tools = _declared_child_tools(
            {
                "skill_view", "read_file", "search_files", "web_search",
                "run_skill_script", "run_skill_python",
            },
            declaration,
            runnable_capability_skills={
                "fda-database", "pubmed-database",
            },
        )

        self.assertIn("web_search", tools)
        self.assertIn("run_skill_python", tools)
        self.assertEqual(
            [],
            _explicit_required_child_capability_tools(declaration, tools),
        )

    def test_nonclinical_worker_only_explicit_invocation_metadata_is_mandatory(self):
        """The availability/mandatory split is domain-neutral."""

        declaration = {
            "tools": [
                {
                    "name": "museum_lookup",
                    "source": "WebSearch",
                },
                {
                    "name": "catalog_normalizer",
                    "tool": "execute_code",
                    "invocation": "required",
                },
                {
                    "name": "optional_export",
                    "tool": "write_file",
                    "mandatory": False,
                },
            ],
        }
        tools = _declared_child_tools(
            {
                "skill_view", "read_file", "search_files", "web_search",
                "execute_code", "write_file",
            },
            declaration,
        )

        self.assertEqual(
            ["execute_code"],
            _explicit_required_child_capability_tools(declaration, tools),
        )
        self.assertIn("web_search", tools)
        self.assertIn("write_file", tools)

    def test_explicit_required_and_mandatory_markers_remain_enforced(self):
        declaration = {
            "tools": [
                {"source": "WebSearch", "required": True},
                {"tool": "execute_code", "mandatory": True},
                {"tool": "write_file", "required": False},
            ],
        }
        child_tools = ["web_search", "execute_code", "write_file"]

        self.assertEqual(
            ["web_search", "execute_code"],
            _explicit_required_child_capability_tools(
                declaration,
                child_tools,
            ),
        )

        self.assertEqual(
            ["web_search"],
            _explicit_required_child_capability_tools(
                {
                    "tool": "web_search",
                    "invocation-required": True,
                },
                child_tools,
            ),
        )

        skill_declaration = {
            "skills": [{
                "skill": "museum-catalog",
                "mandatory": True,
            }],
        }
        skill_tools = _declared_child_tools(
            {"skill_http_get", "run_skill_script", "run_skill_python"},
            skill_declaration,
            needs_prerequisite_reads=False,
            runnable_capability_skills={"museum-catalog"},
            http_capability_skills={"museum-catalog"},
        )
        self.assertEqual(
            ["run_skill_script", "run_skill_python", "skill_http_get"],
            _explicit_required_child_capability_tools(
                skill_declaration,
                skill_tools,
            ),
        )

    def test_bootstrap_primary_source_keeps_acquisition_or_gate(self):
        web_source = {
            "id": "regulatory-guidance",
            "tool": "WebSearch",
            "tools": [
                {"tool": "execute_code"},
            ],
        }
        skill_source = {
            "id": "trial-registry",
            "skill": "skill:clinicaltrials-database",
        }

        self.assertEqual(
            ["web_search"],
            _bootstrap_required_child_capability_tools(
                web_source,
                ["read_file", "web_search", "execute_code"],
            ),
        )
        self.assertEqual(
            ["skill_http_get", "run_skill_script", "run_skill_python"],
            _bootstrap_required_child_capability_tools(
                skill_source,
                [
                    "skill_view", "read_file", "skill_http_get",
                    "run_skill_script", "run_skill_python", "web_search",
                ],
            ),
        )

    def test_first_required_capability_prefers_executable_adapter_tiers(self):
        self.assertEqual(
            _preferred_initial_required_capability_tools([
                "run_skill_script",
                "run_skill_python",
                "skill_http_get",
                "skill_http_post_json",
            ]),
            ["skill_http_get", "skill_http_post_json"],
        )
        self.assertEqual(
            _preferred_initial_required_capability_tools([
                "run_skill_python", "web_search", "skill_http_get",
            ]),
            ["web_search"],
        )
        self.assertEqual(
            _preferred_initial_required_capability_tools([
                "run_skill_script", "run_skill_python",
            ]),
            ["run_skill_script", "run_skill_python"],
        )

    def test_web_only_capability_does_not_gain_python_execution(self):
        tools = _declared_child_tools(
            {
                "skill_view", "read_file", "search_files", "web_search",
                "web_extract", "run_skill_python",
            },
            [{"name": "guideline_search", "source": "WebSearch"}],
        )
        self.assertEqual(
            tools,
            ["skill_view", "read_file", "search_files", "web_search"],
        )
        self.assertNotIn("run_skill_python", tools)

    def test_instruction_only_skill_capability_does_not_gain_ambient_web(self):
        tools = _declared_child_tools(
            {
                "skill_view", "read_file", "search_files", "web_search",
                "web_extract", "run_skill_python", "run_skill_script",
            },
            [{"name": "catalog", "source": "skill:catalog-database"}],
            runnable_capability_skills={"other-database"},
        )
        self.assertEqual(
            tools,
            ["skill_view", "read_file", "search_files"],
        )
        self.assertNotIn("run_skill_python", tools)
        self.assertNotIn("run_skill_script", tools)

    def test_workspace_python_declaration_does_not_gain_function_runner(self):
        tools = _declared_child_tools(
            {
                "skill_view", "read_file", "search_files", "web_search",
                "run_skill_python", "run_skill_script",
            },
            [{"path": "workspace/analyze_results.py", "source": "project"}],
        )

        self.assertNotIn("run_skill_python", tools)
        # Preserve the existing declared-CLI path without upgrading a raw .py
        # string to the Skill-only public-function invocation surface.
        self.assertIn("run_skill_script", tools)

    def test_descriptor_metadata_never_authorizes_native_tools(self):
        tools = _declared_child_tools(
            {
                "web_search", "web_extract", "write_file", "delegate_task",
                "merge_files", "run_skill_script", "run_skill_python",
            },
            [
                {"name": "web_search"},
                {"id": "web_extract"},
                {"path": "write_file"},
                {"file": "delegate_task"},
                {"resource": "merge_files"},
            ],
            needs_prerequisite_reads=False,
        )

        self.assertEqual([], tools)

    def test_descriptor_name_script_suffix_does_not_authorize_runner(self):
        tools = _declared_child_tools(
            {"run_skill_script", "run_skill_python"},
            [{"name": "scripts/fake.py", "source": "project"}],
            needs_prerequisite_reads=False,
        )

        self.assertEqual([], tools)

    def test_explicit_tool_and_capability_fields_authorize_tools(self):
        tools = _declared_child_tools(
            {
                "web_search", "web_extract", "write_file", "delegate_task",
            },
            {
                "tool": "WebSearch",
                "tools": ["web_extract"],
                "capability": "Write",
                "capabilities": {"delegate_task": True},
            },
            needs_prerequisite_reads=False,
        )

        self.assertEqual(
            ["web_search", "web_extract", "write_file", "delegate_task"],
            tools,
        )

    def test_legacy_source_requires_conventional_resolvable_selector(self):
        available = {"web_search", "web_extract", "run_declared_command"}

        self.assertEqual(
            ["web_search"],
            _declared_child_tools(
                available,
                [{"source": "WebSearch"}],
                needs_prerequisite_reads=False,
            ),
        )
        self.assertEqual(
            ["web_extract"],
            _declared_child_tools(
                available,
                [{"source": "via Bash/WebFetch"}],
                needs_prerequisite_reads=False,
            ),
        )
        for source in (
            "project",
            "skill:catalog-database",
            "documentation mentions WebSearch",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    [],
                    _declared_child_tools(
                        available,
                        [{"source": source}],
                        needs_prerequisite_reads=False,
                    ),
                )

    def test_managed_bridge_names_require_backing_grants(self):
        available = {
            "run_skill_script", "run_skill_python",
            "run_declared_command", "skill_http_get",
        }
        unbacked = _declared_child_tools(
            available,
            {"tools": sorted(available)},
            needs_prerequisite_reads=False,
        )
        script_backed = _declared_child_tools(
            available,
            {"tools": ["scripts/render.py"]},
            needs_prerequisite_reads=False,
        )
        command_backed = _declared_child_tools(
            available,
            {"tools": ["Bash(python:*)"]},
            needs_prerequisite_reads=False,
        )
        http_backed = _declared_child_tools(
            available,
            {"skills": ["catalog-database"]},
            needs_prerequisite_reads=False,
            http_capability_skills={"catalog-database"},
        )

        self.assertEqual([], unbacked)
        self.assertEqual(["run_skill_script"], script_backed)
        self.assertEqual(["run_declared_command"], command_backed)
        self.assertEqual(["skill_http_get"], http_backed)

    def test_compiled_allowed_tools_are_explicit_child_authority(self):
        tools = _declared_child_tools(
            {"web_search", "web_extract"},
            {
                "environment_contract": {
                    "allowed_tools": ["WebSearch"],
                },
            },
            needs_prerequisite_reads=False,
        )

        self.assertEqual(["web_search"], tools)

    def test_declared_shell_or_javascript_capability_gets_only_safe_runner(self):
        for script_path in ("scripts/render.sh", "scripts/render.mjs"):
            with self.subTest(script_path=script_path):
                tools = _declared_child_tools(
                    {
                        "skill_view", "read_file", "search_files",
                        "web_search", "run_skill_python", "run_skill_script",
                    },
                    [{"path": script_path, "source": "project"}],
                )

                self.assertIn("run_skill_script", tools)
                self.assertNotIn("run_skill_python", tools)

    def test_proven_python_skill_gets_cli_and_public_function_runners(self):
        available = {
            "skill_view", "read_file", "search_files", "web_search",
            "run_skill_python", "run_skill_script",
        }
        declaration = [{"source": "skill:catalog-database"}]

        generic_only = _declared_child_tools(
            available,
            declaration,
            runnable_script_capability_skills={"catalog-database"},
        )
        python_only = _declared_child_tools(
            available,
            declaration,
            runnable_capability_skills={"catalog-database"},
        )

        self.assertIn("run_skill_script", generic_only)
        self.assertNotIn("run_skill_python", generic_only)
        self.assertIn("run_skill_script", python_only)
        self.assertIn("run_skill_python", python_only)

    def test_cross_skill_names_come_only_from_explicit_skill_declarations(self):
        names = _declared_capability_skill_names({
            "skill": "catalog-database",
            "tools": [
                {"name": "papers", "source": "skill:pubmed-database"},
                {"name": "guidelines", "source": "WebSearch"},
                {"name": "local_rules", "source": "project"},
            ],
            "skills": ["gene-database", "skill:pdb-database"],
        })

        self.assertEqual(
            names,
            [
                "catalog-database",
                "gene-database",
                "pdb-database",
                "pubmed-database",
            ],
        )

    def test_worker_iteration_budget_scales_with_declared_contract(self):
        self.assertEqual(
            _delegated_child_iteration_limit(
                step_type="worker",
                required_output_ids=[f"KG-{index}" for index in range(1, 8)],
                required_capability_skills=[
                    f"database-{index}" for index in range(1, 13)
                ],
            ),
            20,
        )
        self.assertEqual(
            _delegated_child_iteration_limit(
                step_type="knowledge_bootstrap",
                required_output_ids=[],
                required_capability_skills=["catalog-database"],
            ),
            12,
        )

    def test_worker_cross_skill_metadata_cannot_be_removed_or_changed(self):
        policy = _workflow_gate_tool_policy(
            "delegate workflow stage 'research' for session skill 'generic' "
            "(parallel workers: research-a)",
            self.available,
        )
        policy["expected_step_ids"] = ["research-a"]
        policy["expected_required_capability_skills"] = {
            "research-a": ["pubmed-database"],
        }
        exact = {
            "goal": "research",
            "skill_name": "generic",
            "step_type": "worker",
            "step_id": "research-a",
            "tools": ["skill_view", "web_search"],
            "required_capability_skills": ["pubmed-database"],
        }

        self.assertEqual(
            _workflow_gate_call_error(
                policy, "delegate_task", exact, prior_call_count=0
            ),
            "",
        )
        removed = {
            key: value for key, value in exact.items()
            if key != "required_capability_skills"
        }
        self.assertIn(
            "exact required_capability_skills",
            _workflow_gate_call_error(
                policy, "delegate_task", removed, prior_call_count=0
            ),
        )
        changed = {
            **exact,
            "required_capability_skills": ["other-database"],
        }
        self.assertIn(
            "exact required_capability_skills",
            _workflow_gate_call_error(
                policy, "delegate_task", changed, prior_call_count=0
            ),
        )

    def test_worker_capability_audit_metadata_cannot_be_removed_or_changed(self):
        policy = _workflow_gate_tool_policy(
            "delegate workflow stage 'research' for session skill 'generic' "
            "(parallel workers: research-a)",
            self.available,
        )
        policy["expected_step_ids"] = ["research-a"]
        policy["expected_required_capability_tools"] = {
            "research-a": ["web_search"],
        }
        exact = {
            "goal": "research",
            "skill_name": "generic",
            "step_type": "worker",
            "step_id": "research-a",
            "tools": ["skill_view", "web_search"],
            "required_capability_tools": ["web_search"],
        }

        self.assertEqual(
            _workflow_gate_call_error(
                policy, "delegate_task", exact, prior_call_count=0
            ),
            "",
        )
        removed = {key: value for key, value in exact.items() if key != "required_capability_tools"}
        self.assertIn(
            "exact required_capability_tools",
            _workflow_gate_call_error(
                policy, "delegate_task", removed, prior_call_count=0
            ),
        )

    def test_typed_result_fields_compile_and_cannot_be_removed_or_changed(self):
        self.assertEqual(
            _declared_result_field_names({
                "findings": {"type": "array"},
                "evidence": {"type": "array"},
            }),
            ["findings", "evidence"],
        )
        self.assertEqual(
            _declared_result_field_names([
                "title",
                {"id": "enrollment"},
                {"field": "status"},
                "title",
            ]),
            ["title", "enrollment", "status"],
        )
        self.assertEqual(
            _declared_result_field_names({
                "type": "object",
                "properties": {
                    "records": {"type": "array"},
                    "debug": {"type": "string"},
                },
                "required": ["records"],
            }),
            ["records"],
        )
        policy = _workflow_gate_tool_policy(
            "delegate workflow stage 'research' for session skill 'generic' "
            "(parallel workers: research-a)",
            self.available,
        )
        policy["expected_step_ids"] = ["research-a"]
        policy["expected_required_result_fields"] = {
            "research-a": ["findings", "evidence"],
        }
        policy["expected_required_capability_tools"] = {
            "research-a": [],
        }
        exact = {
            "goal": "research",
            "skill_name": "generic",
            "step_type": "worker",
            "step_id": "research-a",
            "tools": ["skill_view", "web_search"],
            "required_result_fields": ["findings", "evidence"],
        }
        self.assertEqual(
            _workflow_gate_call_error(
                policy, "delegate_task", exact, prior_call_count=0
            ),
            "",
        )
        removed = {
            key: value for key, value in exact.items()
            if key != "required_result_fields"
        }
        self.assertIn(
            "exact required_result_fields",
            _workflow_gate_call_error(
                policy, "delegate_task", removed, prior_call_count=0
            ),
        )
        changed = {**exact, "required_result_fields": ["findings"]}
        self.assertIn(
            "exact required_result_fields",
            _workflow_gate_call_error(
                policy, "delegate_task", changed, prior_call_count=0
            ),
        )
        changed = {**exact, "required_capability_tools": ["read_file"]}
        self.assertIn(
            "exact required_capability_tools",
            _workflow_gate_call_error(
                policy, "delegate_task", changed, prior_call_count=0
            ),
        )

    def test_artifact_format_inspection_metadata_must_match_exact_list(self):
        policy = _workflow_gate_tool_policy(
            "generate declared modular/checklist artifacts for session skill 'generic'",
            self.available,
        )
        policy["expected_step_ids"] = ["modular-package"]
        policy["expected_required_capability_tools"] = {
            "modular-package": ["write_file"],
        }
        policy["expected_skill_files_to_inspect"] = {
            "modular-package": ["formats/a.md", "formats/b.md"],
        }
        exact = {
            "goal": "synthesize",
            "skill_name": "generic",
            "step_type": "artifact_synthesis",
            "step_id": "modular-package",
            "tools": ["skill_view", "write_file"],
            "required_capability_tools": ["write_file"],
            "required_skill_files_to_inspect": [
                "formats/a.md", "formats/b.md",
            ],
        }

        self.assertEqual(
            _workflow_gate_call_error(
                policy, "delegate_task", exact, prior_call_count=0
            ),
            "",
        )
        missing = {
            **exact,
            "required_skill_files_to_inspect": ["formats/a.md"],
        }
        self.assertIn(
            "exact required_skill_files_to_inspect",
            _workflow_gate_call_error(
                policy, "delegate_task", missing, prior_call_count=0
            ),
        )

    def test_bootstrap_accepts_exact_intent_selected_skill_resources(self):
        policy = _workflow_gate_tool_policy(
            "delegate knowledge bootstrap for session skill 'generic' "
            "(sources: catalog)",
            self.available,
        )
        policy["expected_step_ids"] = ["catalog"]
        policy["expected_skill_files_to_inspect"] = {
            "catalog": ["domains/cns.md", "phases/all.md"],
        }
        task = {
            "goal": "bootstrap catalog",
            "skill_name": "generic",
            "step_type": "knowledge_bootstrap",
            "step_id": "catalog",
            "tools": ["skill_view", "web_search"],
            "required_skill_files_to_inspect": [
                "domains/cns.md", "phases/all.md",
            ],
        }

        self.assertEqual(
            _workflow_gate_call_error(
                policy, "delegate_task", task, prior_call_count=0
            ),
            "",
        )

    def test_explicit_intent_hints_accept_only_declared_dimension_values(self):
        plan = {
            "intent_classification": {
                "dimensions": [
                    {"id": "task_type", "values": ["lookup", "comprehensive"]},
                    {"id": "phase", "values": ["Phase_I", "all"]},
                ],
            },
        }

        self.assertEqual(
            _explicit_declared_intent_selections(
                plan,
                "Please use these exact selections:\n"
                "task_type=comprehensive\n"
                "phase=all",
            ),
            {"task_type": "comprehensive", "phase": "all"},
        )
        self.assertEqual(
            _explicit_declared_intent_selections(plan, "task_type=invented"),
            {},
        )

    def test_explicit_intent_fast_path_rejects_duplicates_and_choice_context(self):
        plan = {
            "intent_classification": {
                "dimensions": [
                    {"id": "task_type", "values": ["lookup", "comprehensive"]},
                    {"id": "phase", "values": ["Phase_I", "all"]},
                ],
            },
        }

        ambiguous_cases = (
            "task_type=lookup\ntask_type=comprehensive\nphase=all",
            "task_type=lookup\ntask_type=lookup\nphase=all",
            "task_type=lookup or task_type=comprehensive?\nphase=all",
            "Example:\n```text\ntask_type=lookup\nphase=all\n```",
            "do not use task_type=lookup\nphase=all",
        )
        for text in ambiguous_cases:
            with self.subTest(text=text):
                self.assertEqual(
                    _explicit_declared_intent_selections(plan, text),
                    {},
                )

    def test_explicit_intent_block_survives_unrelated_safety_instructions(self):
        plan = {
            "intent_classification": {
                "dimensions": [
                    {"id": "task_type", "values": ["lookup", "comprehensive"]},
                    {"id": "phase", "values": ["Phase_I", "all"]},
                ],
            },
        }

        text = (
            "Execute the declared workflow and do not fabricate evidence.\n"
            "task_type=comprehensive\n"
            "phase=all\n"
            "Do not silently discard unresolved gaps."
        )
        self.assertEqual(
            _explicit_declared_intent_selections(plan, text),
            {"task_type": "comprehensive", "phase": "all"},
        )
        self.assertEqual(
            _explicit_declared_intent_selections(
                plan,
                "Example:\ntask_type=comprehensive\nphase=all",
            ),
            {},
        )


class WorkflowActivationBoundaryTests(unittest.TestCase):
    def test_intent_route_recompile_drops_other_route_tools_and_commands(self):
        worker_a = {
            "id": "route-a",
            "file": "workers/route-a.yaml",
            "tools": ["web_search", "Bash(git status:*)"],
        }
        worker_b = {
            "id": "route-b",
            "file": "workers/route-b.yaml",
            "tools": ["read_file", "Bash(python:*)"],
        }
        execution = {
            "workers": [worker_a, worker_b],
            "routes": [],
            "intent_classification": {
                "dimensions": [{
                    "id": "route",
                    "required": True,
                    "values": ["a", "b"],
                    "mappings": {
                        "worker_map": {
                            "a": ["route-a"],
                            "b": ["route-b"],
                        },
                    },
                }],
            },
        }
        workflow = {
            "workers": [worker_a, worker_b],
            "execution_contract": execution,
        }
        loaded = {
            "router-skill": {
                "name": "router-skill",
                "_chatds_scope": "session",
                "workflow_contract": workflow,
            },
        }
        available = [
            "skills_list", "skill_view", "delegate_task", "web_search",
            "read_file", "run_declared_command",
        ]

        def exposure_for(value: str):
            plan = _build_skill_execution_plan(workflow, "run router-skill")
            resolved = {f"route.worker_map": [f"route-{value}"]}
            _apply_intent_selections_to_plan(
                plan,
                {"route": value},
                resolved,
            )
            plan["resolved_intent_mappings"] = resolved
            return _bounded_skill_execution_exposure(
                "run router-skill",
                available,
                {"router-skill"},
                loaded,
                {},
                selected_skill_names=("router-skill",),
                compiled_plans={"router-skill": plan},
            )

        exposure_a = exposure_for("a")
        exposure_b = exposure_for("b")
        self.assertIn("web_search", exposure_a.tools)
        self.assertNotIn("read_file", exposure_a.tools)
        self.assertIn("read_file", exposure_b.tools)
        self.assertNotIn("web_search", exposure_b.tools)
        self.assertEqual(
            {"git"},
            {item[2] for item in exposure_a.allowed_skill_commands},
        )
        self.assertEqual(
            {"python"},
            {item[2] for item in exposure_b.allowed_skill_commands},
        )
        resources_a = {item[1] for item in exposure_a.allowed_skill_resources}
        resources_b = {item[1] for item in exposure_b.allowed_skill_resources}
        self.assertIn("workers/route-a.yaml", resources_a)
        self.assertNotIn("workers/route-b.yaml", resources_a)
        self.assertIn("workers/route-b.yaml", resources_b)
        self.assertNotIn("workers/route-a.yaml", resources_b)

    def test_selected_resource_closure_keeps_all_33_files(self):
        files = ["orchestration/workflow.yaml"] + [
            f"references/input-{index:03d}.md" for index in range(32)
        ]
        workflow = {
            "orchestrator_files": ["orchestration/workflow.yaml"],
            "execution_contract": {
                "routes": [{
                    "id": "report",
                    "patterns": ["detailed"],
                    "workers": [],
                    "required_files": files,
                }],
            },
        }
        exposure = _bounded_skill_execution_exposure(
            "Use bulk-skill to create a detailed report",
            ["skills_list", "skill_view", "delegate_task", "write_file"],
            {"bulk-skill"},
            {"bulk-skill": {
                "name": "bulk-skill",
                "_chatds_scope": "session",
                "workflow_contract": workflow,
            }},
            {},
        )
        authorized = {
            path for skill, path in exposure.allowed_skill_resources
            if skill == "bulk-skill"
        }
        self.assertTrue(set(files).issubset(authorized))
        self.assertFalse(exposure.missing_requirements)

        state = HarnessRunState(
            original_user_text="Create a detailed report",
            available_tools={"skill_view", "delegate_task", "write_file"},
            session_skill_names={"bulk-skill"},
            skill_workflow_activation="complex_deliverable",
        )
        state.record_skill_view(
            {"name": "bulk-skill"},
            {
                "workflow_contract": workflow,
                "linked_files": {
                    "orchestration": ["orchestration/workflow.yaml"],
                    "references": files[1:],
                },
                "resource_graph": {
                    "categories": {
                        "orchestration": {
                            "sample": ["orchestration/workflow.yaml"]
                        },
                        "references": {"sample": files[1:6]},
                    },
                },
            },
        )
        inspected: list[str] = []
        for _ in files:
            needs_more, reason = state.needs_more_skill_workflow()
            self.assertTrue(needs_more, reason)
            target, error = _compiled_skill_inspection_target(state, reason)
            self.assertFalse(error)
            self.assertIsNotNone(target)
            assert target is not None
            inspected.append(target["file_path"])
            state.record_skill_view(
                {"name": "bulk-skill", "file_path": target["file_path"]},
                {},
            )
        self.assertEqual(files, inspected)
        self.assertEqual((False, ""), state.needs_more_skill_workflow())

    def test_standard_selected_skill_can_progressively_read_bundled_resources(self):
        loaded = {
            "portable-skill": {
                "name": "portable-skill",
                "_chatds_scope": "session",
                "workflow_contract": None,
                "linked_files": {
                    "references": ["references/REFERENCE.md"],
                    "assets": ["assets/template.json"],
                    "scripts": ["scripts/check.py"],
                },
            },
        }

        exposure = _bounded_skill_execution_exposure(
            "Use portable-skill to complete this task",
            ["skills_list", "skill_view", "skill_copy_resource"],
            {"portable-skill"},
            loaded,
            {},
        )

        self.assertFalse(exposure.missing_requirements)
        self.assertEqual(
            {
                "SKILL.md",
                "__manifest__",
            },
            {
                path for skill, path in exposure.allowed_skill_resources
                if skill == "portable-skill"
            },
        )
        # Read authority is not copy or execution authority.
        self.assertNotIn("skill_copy_resource", exposure.tools)
        self.assertFalse(exposure.allowed_skill_scripts)

    def test_standard_instruction_skill_uses_only_user_authorized_actions(self):
        loaded = {
            "portable-skill": {
                "name": "portable-skill",
                "_chatds_scope": "session",
                "workflow_contract": None,
                "linked_files": {
                    "assets": ["assets/brief-template.md"],
                },
            },
        }
        available = [
            "skills_list", "skill_view", "web_search", "write_file",
            "skill_copy_resource", "delegate_task", "execute_code",
        ]

        exposure = _bounded_skill_execution_exposure(
            (
                "Use portable-skill, search the web, and save the result to "
                "brief.md."
            ),
            available,
            {"portable-skill"},
            loaded,
            {},
        )

        self.assertIn("web_search", exposure.tools)
        self.assertIn("write_file", exposure.tools)
        # Package-wide progressive browsing is read-only. Copy authority needs
        # an exact compiled route/output resource and is not inferred from the
        # presence of an asset in the package inventory.
        self.assertNotIn("skill_copy_resource", exposure.tools)
        self.assertNotIn("delegate_task", exposure.tools)
        self.assertNotIn("execute_code", exposure.tools)
        self.assertIn(("web_search",), exposure.required_groups)
        self.assertIn(("write_file",), exposure.required_groups)

    def test_standard_skill_main_directives_compile_closed_capabilities(self):
        from skills.loader import load_skill_content

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "arithmetic-helper"
            (root / "references").mkdir(parents=True)
            (root / "SKILL.md").write_text(
                "---\nname: arithmetic-helper\n"
                "description: Delegate arithmetic and verify the result.\n---\n"
                "# Instructions\n1. Delegate the calculation to a subagent.\n"
                "2. Then verify the result with code execution.\n"
                "```text\nUse write_file to create an example.\n```\n"
                "> Use web_search in a quoted example.\n",
                encoding="utf-8",
            )
            (root / "references" / "notes.md").write_text(
                "Use write_file, web_search, and skill_manage.", encoding="utf-8"
            )
            package = load_skill_content(
                root / "SKILL.md", skill_dir=str(root)
            )

        exposure = _bounded_skill_execution_exposure(
            "Use arithmetic-helper to answer 2+2.",
            ["skills_list", "skill_view", "delegate_task", "execute_code",
             "write_file", "web_search", "skill_manage"],
            {"arithmetic-helper"}, {"arithmetic-helper": package}, {},
        )
        self.assertIn("delegate_task", exposure.tools)
        self.assertIn("execute_code", exposure.tools)
        self.assertNotIn("write_file", exposure.tools)
        self.assertNotIn("web_search", exposure.tools)
        self.assertNotIn("skill_manage", exposure.tools)
        self.assertIn(("delegate_task",), exposure.required_groups)
        self.assertIn(("execute_code",), exposure.required_groups)

    def test_standard_allowed_tools_grant_only_existing_capabilities(self):
        from skills.loader import load_skill_content

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "portable-tools"
            root.mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: portable-tools\n"
                "description: Use explicitly pre-approved portable tools.\n"
                "allowed-tools: Task execute_code Shell(git status:*)\n"
                "---\n# Usage\nFollow the user's request.\n",
                encoding="utf-8",
            )
            package = load_skill_content(
                root / "SKILL.md", skill_dir=str(root)
            )

        exposure = _bounded_skill_execution_exposure(
            "Use portable-tools.",
            ["skills_list", "skill_view", "delegate_task", "execute_code",
             "run_declared_command", "write_file"],
            {"portable-tools"}, {"portable-tools": package}, {},
        )
        self.assertIn("delegate_task", exposure.tools)
        self.assertIn("execute_code", exposure.tools)
        self.assertIn("run_declared_command", exposure.tools)
        self.assertNotIn("write_file", exposure.tools)
        self.assertEqual("git", exposure.allowed_skill_commands[0][2])

    def test_standard_skill_explicit_empty_allowed_tools_stays_closed(self):
        workflow = {
            "execution_contract": {
                "schema_version": 1,
                "environment_contract": {
                    "allowed_tools": [],
                    "allowed_tool_groups": [{
                        "source_file": "SKILL.md",
                        "explicit_empty": True,
                    }],
                },
            },
        }
        loaded = {
            "closed-skill": {
                "name": "closed-skill",
                "_chatds_scope": "session",
                "content": (
                    "# Instructions\nDelegate to a subagent, then verify with "
                    "code execution."
                ),
                "workflow_contract": workflow,
                "linked_files": {},
            },
        }

        exposure = _bounded_skill_execution_exposure(
            "Use closed-skill and search the web for current sources.",
            ["skills_list", "skill_view", "web_search", "delegate_task"],
            {"closed-skill"},
            loaded,
            {},
        )

        self.assertEqual(("skills_list", "skill_view"), exposure.tools)
        self.assertFalse(exposure.required_groups)

    def test_selected_resource_closure_over_256_fails_before_mutation(self):
        files = [f"references/input-{index:03d}.md" for index in range(257)]
        workflow = {
            "execution_contract": {
                "routes": [{
                    "id": "report",
                    "patterns": ["detailed"],
                    "workers": [],
                    "required_files": files,
                }],
            },
        }
        exposure = _bounded_skill_execution_exposure(
            "Use bulk-skill to create a detailed report",
            [
                "skills_list", "skill_view", "delegate_task", "write_file",
                "patch_file", "run_skill_script", "run_declared_command",
            ],
            {"bulk-skill"},
            {"bulk-skill": {
                "name": "bulk-skill",
                "_chatds_scope": "session",
                "workflow_contract": workflow,
            }},
            {},
        )
        self.assertTrue(any(
            "resource_closure_limit" in item
            and "257 files" in item
            for item in exposure.missing_requirements
        ))
        self.assertTrue(set(exposure.tools).issubset({"skills_list", "skill_view"}))
        self.assertFalse(exposure.allowed_skill_scripts)
        self.assertFalse(exposure.allowed_skill_commands)

        state = HarnessRunState(
            original_user_text="Create a detailed report",
            available_tools={"skill_view", "delegate_task", "write_file"},
            skill_workflow_activation="complex_deliverable",
        )
        state.record_skill_view(
            {"name": "bulk-skill"},
            {"workflow_contract": workflow},
        )
        plan = state.skill_execution_plans["bulk-skill"]
        self.assertEqual("invalid_contract", plan["selection"])
        self.assertIn("257 files", plan["resource_closure_error"])

    def test_capability_skill_linked_files_do_not_become_ambient_child_closure(self):
        worker = {
            "id": "writer",
            "file": "workers/writer.yaml",
            "skills": ["schema-helper"],
        }
        workflow = {
            "workers": [worker],
            "execution_contract": {
                "workers": [worker],
                "routes": [{
                    "id": "run",
                    "patterns": ["run"],
                    "workers": ["writer"],
                }],
            },
        }
        loaded = {
            "parent-workflow": {
                "name": "parent-workflow",
                "_chatds_scope": "session",
                "workflow_contract": workflow,
            },
            "schema-helper": {
                "name": "schema-helper",
                "_chatds_scope": "session",
                "workflow_contract": None,
                "linked_files": {
                    "references": ["references/schema.md"],
                    "templates": ["templates/record.json"],
                },
            },
            "unrelated-helper": {
                "name": "unrelated-helper",
                "_chatds_scope": "session",
                "workflow_contract": None,
                "linked_files": {"references": ["references/other.md"]},
            },
        }

        exposure = _bounded_skill_execution_exposure(
            "run parent-workflow",
            ["skills_list", "skill_view", "delegate_task"],
            set(loaded),
            loaded,
            {},
        )

        self.assertFalse(exposure.missing_requirements)
        self.assertIn("delegate_task", exposure.tools)
        capability_resources = {
            path for skill, path in exposure.allowed_skill_resources
            if skill == "schema-helper"
        }
        self.assertEqual({"SKILL.md"}, capability_resources)
        self.assertFalse(any(
            skill == "unrelated-helper"
            for skill, _path in exposure.allowed_skill_resources
        ))

    def test_capability_inventory_is_safe_but_size_is_not_execution_authority(self):
        worker = {
            "id": "writer",
            "file": "workers/writer.yaml",
            "skills": ["schema-helper"],
        }
        workflow = {
            "workers": [worker],
            "execution_contract": {
                "workers": [worker],
                "routes": [{
                    "id": "run",
                    "patterns": ["run"],
                    "workers": ["writer"],
                }],
            },
        }

        for label, linked_files, expected_error in (
            (
                "traversal",
                {"references": ["../secret.md"]},
                "unsafe supporting resource",
            ),
            (
                "limit",
                {
                    "references": [
                        f"references/item-{index:04d}.md"
                        for index in range(513)
                    ]
                },
                None,
            ),
        ):
            with self.subTest(label=label):
                loaded = {
                    "parent-workflow": {
                        "name": "parent-workflow",
                        "_chatds_scope": "session",
                        "workflow_contract": workflow,
                    },
                    "schema-helper": {
                        "name": "schema-helper",
                        "_chatds_scope": "session",
                        "workflow_contract": None,
                        "linked_files": linked_files,
                    },
                }
                exposure = _bounded_skill_execution_exposure(
                    "run parent-workflow",
                    [
                        "skills_list", "skill_view", "delegate_task",
                        "write_file", "run_skill_script",
                    ],
                    set(loaded),
                    loaded,
                    {},
                )
                matching = [
                    item for item in exposure.missing_requirements
                    if "capability_skill_resource_closure_invalid" in item
                ]
                if expected_error:
                    self.assertTrue(any(expected_error in item for item in matching))
                    self.assertTrue(
                        set(exposure.tools).issubset({"skills_list", "skill_view"})
                    )
                else:
                    self.assertFalse(matching)
                    self.assertIn("delegate_task", exposure.tools)
                    self.assertEqual(
                        {"SKILL.md"},
                        {
                            path for skill, path in exposure.allowed_skill_resources
                            if skill == "schema-helper"
                        },
                    )
                self.assertFalse(exposure.allowed_skill_scripts)

    def test_complex_skill_execution_boundary_requires_deterministic_selection(self):
        def package(pattern: str) -> dict:
            return {
                "workflow_contract": {
                    "execution_contract": {
                        "routes": [{
                            "id": "matched-route",
                            "patterns": [pattern],
                            "workers": [],
                        }],
                    },
                },
            }

        packages = {
            "oncology-report": package("oncology"),
            "finance-report": package("finance"),
        }
        names = set(packages)
        self.assertEqual(
            ("oncology-report",),
            _deterministic_complex_skill_selection(
                "Create a detailed oncology report",
                names,
                "oncology-report",
                packages,
            ),
        )
        self.assertEqual(
            (),
            _deterministic_complex_skill_selection(
                "Create a detailed oncology report",
                names,
                "finance-report",
                packages,
            ),
        )
        ambiguous = {
            "a": package("report"),
            "b": package("report"),
        }
        self.assertEqual(
            (),
            _deterministic_complex_skill_selection(
                "Create a detailed report", set(ambiguous), "a", ambiguous
            ),
        )

        # A valid instruction-only package can be selected from canonical
        # name+description metadata without a domain-specific route table.
        description_packages = {
            "collection-reconciler": {
                "description": (
                    "Reconcile museum collection accession, provenance, and "
                    "conservation records."
                ),
                "workflow_contract": None,
            },
            "launch-readiness": {
                "description": (
                    "Assess satellite telemetry, orbital safety, and weather windows."
                ),
                "workflow_contract": None,
            },
        }
        description_request = (
            "Build a comprehensive museum inventory reconciliation report "
            "covering accession and provenance records."
        )
        self.assertEqual(
            ("collection-reconciler",),
            _deterministic_complex_skill_selection(
                description_request,
                set(description_packages),
                "collection-reconciler",
                description_packages,
            ),
        )
        self.assertEqual(
            (),
            _deterministic_complex_skill_selection(
                description_request,
                set(description_packages),
                "launch-readiness",
                description_packages,
            ),
        )

    def test_single_explicit_ingress_skill_survives_generic_use_skill_wording(self):
        packages = {
            "visual-browser-operator": {
                "description": (
                    "Inspect rendered interfaces with ordinary browser actions."
                ),
                "workflow_contract": None,
            },
        }
        request = (
            "http://172.30.100.145:5173/chat/example "
            "使用skill访问这个网站，说明这个网站的内容"
        )
        # Without an ingress receipt, unrelated generic wording remains
        # fail-closed rather than guessing from a sole catalog entry.
        self.assertEqual(
            (),
            _deterministic_complex_skill_selection(
                request,
                set(packages),
                "visual-browser-operator",
                packages,
            ),
        )
        # The exact single selection already compiled from the user's action
        # clause may advance to digest binding and typed capability planning.
        self.assertEqual(
            ("visual-browser-operator",),
            _deterministic_complex_skill_selection(
                request,
                set(packages),
                "visual-browser-operator",
                packages,
                explicit_selected_skill_names=("visual-browser-operator",),
            ),
        )

    def test_session_skill_description_selector_is_cross_domain_and_fail_closed(self):
        catalog = {
            "launch-readiness": {
                "description": (
                    "Assess satellite launch telemetry, orbital safety, and "
                    "weather windows. 评估卫星发射遥测、轨道安全和气象窗口。"
                ),
            },
            "collection-reconciler": {
                "description": (
                    "Reconcile museum collection accession, provenance, and "
                    "conservation records. 核对博物馆藏品登记、来源和保护记录。"
                ),
            },
            "invoice-audit": {
                "description": (
                    "Audit supplier invoices, purchase orders, tax, and "
                    "duplicate payments. 审计供应商发票、采购订单和重复付款。"
                ),
            },
        }
        cases = {
            (
                "Create a comprehensive launch readiness report using satellite "
                "telemetry and weather windows."
            ): "launch-readiness",
            (
                "Build a detailed museum inventory reconciliation report covering "
                "accession and provenance records."
            ): "collection-reconciler",
            (
                "Use invoice-audit to generate a full supplier duplicate-payment report."
            ): "invoice-audit",
            "制定详细的博物馆藏品登记和来源核对方案": "collection-reconciler",
        }
        for request, expected in cases.items():
            with self.subTest(request=request):
                decision = _bounded_session_skill_relevance_selection(
                    request,
                    catalog,
                )
                self.assertEqual((expected,), decision.selected_skill_names)
                self.assertEqual("selected", decision.reason)

        weak = _bounded_session_skill_relevance_selection(
            "Generate a comprehensive analysis report.",
            catalog,
        )
        self.assertEqual((), weak.selected_skill_names)
        self.assertEqual("below_absolute_threshold", weak.reason)

        opted_out = _bounded_session_skill_relevance_selection(
            "Create a satellite telemetry report, but do not use any Skill.",
            catalog,
        )
        self.assertEqual((), opted_out.selected_skill_names)
        self.assertEqual("user_opted_out", opted_out.reason)

        oversized = _bounded_session_skill_relevance_selection(
            "Create a comprehensive report.",
            {
                f"bounded-skill-{index}": {"description": "bounded catalog"}
                for index in range(129)
            },
        )
        self.assertEqual((), oversized.selected_skill_names)
        self.assertEqual("catalog_limit_exceeded", oversized.reason)

    def test_session_skill_description_selector_rejects_ambiguous_catalog(self):
        catalog = {
            "inventory-alpha": {
                "description": (
                    "Analyze tabular inventory records and produce a detailed report."
                ),
            },
            "inventory-beta": {
                "description": (
                    "Review tabular inventory records and produce a complete report."
                ),
            },
        }
        decision = _bounded_session_skill_relevance_selection(
            "Create a detailed report for tabular inventory records.",
            catalog,
        )

        self.assertEqual((), decision.selected_skill_names)
        self.assertEqual("insufficient_top_margin", decision.reason)
        self.assertEqual(decision.ranked_scores[0][1], decision.ranked_scores[1][1])

    def test_session_skill_relevance_grants_only_exact_main_document_read(self):
        exposure = _session_skill_relevance_inspection_exposure([
            "skill_view",
            "skills_list",
            "run_skill_python",
            "run_skill_script",
            "run_declared_command",
            "skill_http_get",
            "delegate_task",
            "write_file",
            "execute_code",
        ])

        self.assertEqual(("skill_view",), exposure.tools)
        self.assertEqual((("skill_view",),), exposure.required_groups)
        self.assertEqual((), exposure.missing_requirements)

    def test_model_catalog_has_one_canonical_default_and_non_catalog_alias(self):
        self.assertEqual(
            [
                model_id for model_id, config in PROVIDERS.items()
                if config.get("is_default") is True
            ],
            [DEFAULT_AGENT_MODEL_ID],
        )
        self.assertEqual(
            DEFAULT_AGENT_MODEL_ID,
            canonical_provider_id("AgentModel"),
        )
        self.assertNotIn("AgentModel", PROVIDERS)

    def test_domain_words_and_explanation_requests_remain_direct_chat(self):
        direct_requests = (
            "详细分析这份临床试验报告是什么意思",
            "请解释这份 regulatory strategy report",
            "Please provide an explanation of this clinical report",
            "这份全面设计方案该如何理解？",
            "识别这张图片里的文字并告诉我内容",
            "provide a study overview",
            "帮我制定一个三天学习计划",
            "给我一个减脂计划",
            "请提供一个简单方案",
            "Create a study plan",
            "Draft a trial strategy",
            "制定临床试验方案",
            "制定研究计划",
            "What does museum collection provenance mean?",
        )
        for text in direct_requests:
            with self.subTest(text=text):
                self.assertFalse(_looks_like_complex_artifact_request(text))
                self.assertEqual(
                    _skill_workflow_activation_for_request(text),
                    "inactive",
                )

    def test_complex_outputs_and_explicit_skill_requests_activate(self):
        complex_requests = (
            "请制定完整的临床试验方案",
            "Generate a comprehensive regulatory strategy report",
            "task_type=comprehensive_design\n请生成完整报告",
            "生成一个分析报告",
            "生成一份数据分析报告",
            "Generate a comprehensive analysis plan",
            "制定一份全面分析方案",
            "Create a detailed analysis strategy report",
            "Generate a full analysis protocol",
            "生成完整分析方案",
            "Create a museum reconciliation plan with a multi-step workflow",
            "Build an inventory strategy using database queries and code execution",
            "Generate a detailed satellite launch readiness plan",
        )
        for text in complex_requests:
            with self.subTest(text=text):
                self.assertTrue(_looks_like_complex_artifact_request(text))
                self.assertEqual(
                    _skill_workflow_activation_for_request(text),
                    "complex_deliverable",
                )

        for text in (
            "Analyse this plan and tell me what it means",
            "请分析这份方案并告诉我结论",
            "请提供对这份完整方案的分析",
        ):
            with self.subTest(input_analysis=text):
                self.assertFalse(_looks_like_complex_artifact_request(text))

        # One ordinary durable file action remains direct and receives an
        # action-scoped file surface instead of activating every Skill/tool.
        simple_file_request = "把最终结果保存为 workspace/final.md"
        self.assertFalse(_looks_like_complex_artifact_request(simple_file_request))
        self.assertEqual(
            _skill_workflow_activation_for_request(simple_file_request),
            "inactive",
        )

        self.assertTrue(_explicit_skill_workflow_request("请按照上传的 Skill 执行这个任务"))
        self.assertEqual(
            _skill_workflow_activation_for_request("请按照上传的 Skill 执行这个任务"),
            "explicit_skill_request",
        )
        self.assertFalse(_explicit_skill_workflow_request("如何使用 Skill？"))
        self.assertFalse(_explicit_skill_workflow_request("不要使用任何 Skill"))
        self.assertFalse(_explicit_skill_workflow_request("不需要使用 Skill，直接回答"))
        self.assertFalse(_explicit_skill_workflow_request("No need to use a Skill; just answer"))
        self.assertFalse(_explicit_skill_workflow_request("I don't want to use a Skill"))
        self.assertFalse(_explicit_skill_workflow_request("不要使用 $generic-research-pipeline"))
        self.assertTrue(_explicit_skill_workflow_request("$generic-research-pipeline"))
        self.assertFalse(_explicit_skill_workflow_request("我刚刚又执行了一次 Skill 测试，为什么会报错？"))

    def test_exact_available_skill_name_requires_an_action_phrase(self):
        names = {"generic-research-pipeline"}
        positive = (
            "请运行 generic-research-pipeline",
            "按 generic-research-pipeline 执行",
            "use generic-research-pipeline for this request",
        )
        for text in positive:
            with self.subTest(text=text):
                self.assertTrue(_explicit_skill_workflow_request(text, names))
                self.assertEqual(
                    _skill_workflow_activation_for_request(text, names),
                    "explicit_skill_request",
                )

        for text in (
            "generic-research-pipeline 是什么？",
            "为什么 generic-research-pipeline 会被调用？",
            "不要运行 generic-research-pipeline",
            "我不想使用 generic-research-pipeline",
            "I don't want to use generic-research-pipeline",
        ):
            with self.subTest(text=text):
                self.assertFalse(_explicit_skill_workflow_request(text, names))

        # Negation is scoped to one clause; a later explicit request for a
        # different Skill must still be honored.
        self.assertTrue(_explicit_skill_workflow_request(
            "不要运行 other-pipeline；请运行 generic-research-pipeline",
            names,
        ))

    def test_accidental_skill_view_cannot_activate_simple_chat_gate(self):
        state = HarnessRunState(
            user_id="u",
            session_id="s",
            original_user_text="这张图片里写了什么？",
            session_skill_names={"unrelated-workflow"},
            available_tools={"skill_view", "delegate_task"},
            skill_workflow_activation="inactive",
        )
        state.record_skill_view(
            {"name": "unrelated-workflow"},
            {
                "linked_files": {
                    "orchestration": ["orchestration/main.md"],
                    "workers": ["workers/research.md"],
                },
                "workflow_contract": {
                    "workflow_files": ["orchestration/main.md"],
                    "worker_files": ["workers/research.md"],
                    "requires_worker_outputs": True,
                },
            },
        )

        self.assertFalse(state.skill_workflow_is_active())
        self.assertEqual(state.execution_mode(), "direct_chat")
        self.assertEqual(
            state.workflow_debug_snapshot()["workflow_activation"],
            {
                "state": "inactive",
                "active": False,
                "enforced": True,
                "execution_mode": "direct_chat",
            },
        )
        self.assertEqual(state.needs_more_skill_workflow(), (False, ""))
        self.assertFalse(_complex_report_context(state, state.original_user_text))

    def test_optional_tool_failure_does_not_promote_inactive_chat(self):
        state = HarnessRunState(
            original_user_text="这张图是什么？",
            skill_workflow_activation="inactive",
        )
        state.viewed_skill_names.add("irrelevant")
        state.tool_error_count = 1
        state.last_tool_error_at = 1

        self.assertFalse(_should_recover_tool_failure(state, state.original_user_text))

        state.skill_workflow_activation = "explicit_skill_request"
        state.original_user_text = "请总结该 Skill 的说明文档"
        self.assertFalse(_should_recover_tool_failure(
            state, state.original_user_text,
        ))

    def test_explicit_skill_relevance_does_not_force_multi_agent_complexity(self):
        state = HarnessRunState(
            original_user_text="请总结 generic-research-pipeline Skill 的说明文档",
            session_skill_names={"generic-research-pipeline"},
            skill_workflow_activation="explicit_skill_request",
        )
        self.assertFalse(state.skill_workflow_is_active())
        self.assertEqual("skill_direct_chat", state.execution_mode())
        exposure = _skill_documentation_tool_exposure(
            ["skills_list", "skill_view", "delegate_task", "web_search"],
        )
        self.assertEqual({"skills_list", "skill_view"}, set(exposure.tools))
        self.assertEqual((("skill_view",),), exposure.required_groups)

        state.original_user_text = (
            "请使用 generic-research-pipeline 生成完整的多阶段分析报告"
        )
        self.assertTrue(state.skill_workflow_is_active())
        self.assertEqual("skill_workflow", state.execution_mode())

    def test_database_skill_query_uses_declared_runner_not_query_word_web(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        (root / "scripts").mkdir()
        (root / "SKILL.md").write_text(
            "# Catalog\nRun `scripts/query.sh`.\n",
            encoding="utf-8",
        )
        script = root / "scripts/query.sh"
        script.write_text("#!/bin/sh\nprintf query\n", encoding="utf-8")
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        scripts = (("scripts/query.sh", digest),)
        loaded = {
            "name": "catalog-database",
            "skill_dir": str(root),
            "skill_md_sha256": hashlib.sha256(
                (root / "SKILL.md").read_bytes()
            ).hexdigest(),
            "runtime_profile_manifest": (
                compile_skill_runtime_profile_manifest(
                    root,
                    ("scripts/query.sh",),
                )
            ),
            "linked_files": {"scripts": ["scripts/query.sh"]},
            "workflow_contract": {
                "script_candidates": ["scripts/query.sh"],
                "resource_authority": {
                    "reasons": {
                        "scripts/query.sh": [
                            "explicit_skill_reference",
                        ],
                    },
                },
            },
        }
        exposure = _bounded_skill_execution_exposure(
            "请使用 catalog-database 查询 SKU-42",
            [
                "skills_list", "skill_view", "run_skill_script",
                "run_skill_python", "web_search", "delegate_task",
            ],
            {"catalog-database"},
            {"catalog-database": loaded},
            {"catalog-database": scripts},
        )

        self.assertEqual(("catalog-database",), exposure.selected_skills)
        self.assertIn("run_skill_script", exposure.tools)
        self.assertNotIn("web_search", exposure.tools)
        self.assertNotIn("delegate_task", exposure.tools)
        self.assertEqual(
            (("run_skill_script",),), exposure.required_groups,
        )
        self.assertEqual(
            (("catalog-database", "scripts/query.sh", digest),),
            exposure.allowed_skill_scripts,
        )
        self.assertTrue(exposure.allowed_skill_script_authorities)
        self.assertTrue(exposure.allowed_skill_package_digests)

        web_augmented = _bounded_skill_execution_exposure(
            (
                "请使用 catalog-database 查询 SKU-42，并另外联网搜索最新的"
                "公开价格。"
            ),
            [
                "skills_list", "skill_view", "run_skill_script",
                "web_search", "delegate_task",
            ],
            {"catalog-database"},
            {"catalog-database": loaded},
            {"catalog-database": scripts},
        )
        self.assertIn("run_skill_script", web_augmented.tools)
        self.assertIn("web_search", web_augmented.tools)
        self.assertIn(("web_search",), web_augmented.required_groups)

    def test_pure_function_skill_exposes_exact_python_entrypoint(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        (root / "scripts").mkdir()
        (root / "SKILL.md").write_text(
            "# Math\nRun `scripts/normalize.py`.\n",
            encoding="utf-8",
        )
        script = root / "scripts/normalize.py"
        script.write_text(
            "def normalize_score(value):\n    return value\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        scripts = (("scripts/normalize.py", digest),)
        exposure = _bounded_skill_execution_exposure(
            "Use math-functions to call normalize_score(7)",
            [
                "skills_list", "skill_view", "run_skill_script",
                "run_skill_python", "web_search", "delegate_task",
            ],
            {"math-functions"},
            {
                "math-functions": {
                    "name": "math-functions",
                    "skill_dir": str(root),
                    "skill_md_sha256": hashlib.sha256(
                        (root / "SKILL.md").read_bytes()
                    ).hexdigest(),
                    "runtime_profile_manifest": (
                        compile_skill_runtime_profile_manifest(
                            root,
                            ("scripts/normalize.py",),
                        )
                    ),
                    "linked_files": {"scripts": ["scripts/normalize.py"]},
                    "workflow_contract": {
                        "script_candidates": [
                            "scripts/normalize.py",
                        ],
                        "resource_authority": {
                            "reasons": {
                                "scripts/normalize.py": [
                                    "explicit_skill_reference",
                                ],
                            },
                        },
                    },
                },
            },
            {"math-functions": scripts},
        )

        self.assertIn("run_skill_python", exposure.tools)
        self.assertIn("run_skill_script", exposure.tools)
        self.assertNotIn("web_search", exposure.tools)
        self.assertEqual(
            (("math-functions", "scripts/normalize.py", digest),),
            exposure.allowed_skill_scripts,
        )

    def test_structured_mixed_runtime_grants_and_child_routing_are_exact(
        self,
    ):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        (root / "scripts").mkdir()
        (root / "orchestration").mkdir()
        skill_text = "# Mixed\nUse `orchestration/main.yaml`.\n"
        (root / "SKILL.md").write_text(skill_text, encoding="utf-8")
        declaration = root / "orchestration/main.yaml"
        declaration.write_text(
            "scripts:\n"
            "  - scripts/base.cjs\n"
            "  - scripts/browser.cjs\n",
            encoding="utf-8",
        )
        base = root / "scripts/base.cjs"
        browser = root / "scripts/browser.cjs"
        base.write_text(
            'const fs = require("fs");\nmodule.exports = fs;\n',
            encoding="utf-8",
        )
        browser.write_text(
            'const { chromium } = require("playwright");\n'
            "module.exports = chromium;\n",
            encoding="utf-8",
        )
        (root / "package.json").write_text(
            json.dumps({
                "dependencies": {"playwright": "^1.60.0"},
            }),
            encoding="utf-8",
        )
        base_digest = hashlib.sha256(base.read_bytes()).hexdigest()
        browser_digest = hashlib.sha256(browser.read_bytes()).hexdigest()
        script_paths = (
            "scripts/base.cjs",
            "scripts/browser.cjs",
        )
        workflow = {
            "script_candidates": list(script_paths),
            "resource_authority": {
                "reasons": {
                    path: ["declared_by:orchestration/main.yaml"]
                    for path in script_paths
                },
            },
            "execution_contract": {
                "schema_version": 1,
                "environment_contract": {},
            },
        }
        manifest = compile_skill_runtime_profile_manifest(
            root,
            script_paths,
        )
        loaded = {
            "name": "mixed-runtime",
            "skill_dir": str(root),
            "skill_md_sha256": hashlib.sha256(
                (root / "SKILL.md").read_bytes()
            ).hexdigest(),
            "runtime_profile_manifest": manifest,
            "workflow_contract": workflow,
            "linked_files": {
                "scripts": list(script_paths),
                "orchestration": ["orchestration/main.yaml"],
            },
        }
        plan = {
            "selection": "selected",
            "route_id": "mixed",
            "workers": {},
            "required_workers": [],
            "bootstrap_sources": [{
                "id": "run-both",
                "local_resources": list(script_paths),
            }],
            "aggregation_steps": [],
        }
        exposure = _bounded_skill_execution_exposure(
            "Use mixed-runtime",
            [
                "skill_view",
                "run_skill_process",
                "run_skill_script",
                "run_skill_python",
                "delegate_task",
            ],
            {"mixed-runtime"},
            {"mixed-runtime": loaded},
            {
                "mixed-runtime": (
                    ("scripts/base.cjs", base_digest),
                    ("scripts/browser.cjs", browser_digest),
                ),
            },
            selected_skill_names=("mixed-runtime",),
            compiled_plans={"mixed-runtime": plan},
        )

        base_grant = (
            "mixed-runtime",
            "scripts/base.cjs",
            base_digest,
        )
        browser_grant = (
            "mixed-runtime",
            "scripts/browser.cjs",
            browser_digest,
        )
        self.assertEqual(
            {base_grant, browser_grant},
            set(exposure.allowed_skill_scripts),
        )
        self.assertEqual(
            (browser_grant,),
            exposure.process_only_skill_scripts,
        )
        self.assertIn("run_skill_process", exposure.tools)
        self.assertIn("run_skill_script", exposure.tools)
        self.assertEqual(
            {("mixed-runtime", manifest["package_sha256"])},
            set(exposure.allowed_skill_package_digests),
        )
        self.assertEqual(
            {"orchestration/main.yaml"},
            {
                row[2]
                for row in exposure.allowed_skill_script_authorities
            },
        )

        context = ToolContext(
            user_id="u",
            session_id="s",
            skill_execution_resource_boundary=True,
            allowed_skill_scripts=exposure.allowed_skill_scripts,
            process_only_skill_scripts=(
                exposure.process_only_skill_scripts
            ),
            allowed_skill_script_authorities=(
                exposure.allowed_skill_script_authorities
            ),
            allowed_skill_package_digests=(
                exposure.allowed_skill_package_digests
            ),
        )
        mixed_child_tools = _declared_child_tools(
            {
                "run_skill_process",
                "run_skill_script",
                "run_skill_python",
            },
            {"skill": "mixed-runtime"},
            **_profile_bound_child_runner_kwargs(
                context,
                ["mixed-runtime"],
            ),
        )
        self.assertEqual(
            {"run_skill_process", "run_skill_script"},
            set(mixed_child_tools),
        )
        browser_local_tools = _declared_child_tools(
            {
                "run_skill_process",
                "run_skill_script",
                "run_skill_python",
            },
            {"local_resources": ["scripts/browser.cjs"]},
            **_profile_bound_child_runner_kwargs(
                context,
                [],
                local_skill_name="mixed-runtime",
            ),
        )
        self.assertEqual(
            ["run_skill_process"],
            browser_local_tools,
        )

        with patch(
            "skills.scanner.resolve_skill_path",
            return_value=root / "SKILL.md",
        ):
            self.assertIsNone(delegated_resource_boundary_error(
                "run_skill_script",
                {"script_path": "skills/mixed-runtime/scripts/base.cjs"},
                context,
            ))
            browser_error = delegated_resource_boundary_error(
                "run_skill_script",
                {
                    "script_path": (
                        "skills/mixed-runtime/scripts/browser.cjs"
                    ),
                },
                context,
            )
            self.assertIn("only through run_skill_process", browser_error)
            self.assertIsNone(delegated_resource_boundary_error(
                "run_skill_process",
                {
                    "operation": "start",
                    "script_path": (
                        "skills/mixed-runtime/scripts/browser.cjs"
                    ),
                },
                context,
            ))

        # The old loaded contract/manifest cannot mint fresh authority from a
        # later package snapshot, even when both script bytes are unchanged.
        (root / "SKILL.md").write_text(
            skill_text + "\nChanged.\n",
            encoding="utf-8",
        )
        root_changed = _bounded_skill_execution_exposure(
            "Use mixed-runtime",
            exposure.tools,
            {"mixed-runtime"},
            {"mixed-runtime": loaded},
            {
                "mixed-runtime": (
                    ("scripts/base.cjs", base_digest),
                    ("scripts/browser.cjs", browser_digest),
                ),
            },
            selected_skill_names=("mixed-runtime",),
            compiled_plans={"mixed-runtime": plan},
        )
        self.assertFalse(root_changed.allowed_skill_scripts)
        (root / "SKILL.md").write_text(skill_text, encoding="utf-8")
        declaration.write_text(
            "scripts: []\n",
            encoding="utf-8",
        )
        changed = _bounded_skill_execution_exposure(
            "Use mixed-runtime",
            exposure.tools,
            {"mixed-runtime"},
            {"mixed-runtime": loaded},
            {
                "mixed-runtime": (
                    ("scripts/base.cjs", base_digest),
                    ("scripts/browser.cjs", browser_digest),
                ),
            },
            selected_skill_names=("mixed-runtime",),
            compiled_plans={"mixed-runtime": plan},
        )
        self.assertFalse(changed.allowed_skill_scripts)
        self.assertTrue(any(
            "loaded_skill_snapshot_authority_mismatch" in item
            for item in changed.missing_requirements
        ))

    def test_declared_result_schema_preserves_non_research_native_types(self):
        schema = {
            "type": "object",
            "properties": {
                "sum": {"type": "number"},
                "rows": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["sum", "rows"],
        }

        self.assertEqual(["sum", "rows"], _declared_result_field_names(schema))
        self.assertEqual(
            {
                "sum": {"type": "number"},
                "rows": {"type": "array", "items": {"type": "integer"}},
            },
            _declared_result_field_schema(schema),
        )

    def test_descriptor_list_result_schemas_separate_metadata_from_value_shape(self):
        self.assertEqual(
            {"title": {}},
            _declared_result_field_schema(["title"]),
        )
        self.assertEqual(
            {"title": {}},
            _declared_result_field_schema([{
                "field": "title",
                "description": "Human-readable title",
                "example": "Release notes",
                "label": "Title",
            }]),
        )
        self.assertEqual(
            {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                    "description": "Exact filesystem rows",
                },
            },
            _declared_result_field_schema([{
                "field": "rows",
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "description": "Exact filesystem rows",
                "label": "Rows",
            }]),
        )

        explicit = {
            "type": "string",
            "enum": ["ready", "blocked"],
            "description": "Exact terminal state",
        }
        self.assertEqual(
            {"state": explicit},
            _declared_result_field_schema([{
                "key": "state",
                "schema": explicit,
                "label": "State",
            }]),
        )

    def test_descriptor_list_result_schema_unknown_controls_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported_control"):
            _declared_result_field_schema([{
                "field": "value",
                "unsupported_control": "string",
            }])
        with self.assertRaisesRegex(ValueError, "conflicts with.*explicit schema"):
            _declared_result_field_schema([{
                "field": "value",
                "schema": {"type": "string"},
                "type": "number",
            }])

    def test_optional_json_schema_properties_do_not_become_required_fields(self):
        schema = {
            "type": "object",
            "properties": {
                "optional_title": {"type": "string"},
                "optional_count": {"type": "integer"},
            },
        }

        self.assertEqual([], _declared_result_field_names(schema))
        self.assertEqual({}, _declared_result_field_schema(schema))
        without_type = {"properties": dict(schema["properties"])}
        self.assertEqual([], _declared_result_field_names(without_type))
        self.assertEqual({}, _declared_result_field_schema(without_type))

    def test_declared_result_schema_preserves_supported_fragments_losslessly(self):
        nested_properties = {
            f"field_{index}": {
                "type": "string",
                "description": "d" * 40,
            }
            for index in range(70)
        }
        field_schema = {
            "type": "object",
            "properties": nested_properties,
            "required": list(nested_properties),
            "additionalProperties": False,
            "description": "x" * 3_000,
        }
        schema = {
            "type": "object",
            "properties": {"payload": field_schema},
            "required": ["payload"],
        }

        compiled = _declared_result_field_schema(schema)

        self.assertEqual(field_schema, compiled["payload"])
        self.assertEqual(70, len(compiled["payload"]["properties"]))
        self.assertEqual(3_000, len(compiled["payload"]["description"]))

    def test_declared_result_schema_fails_instead_of_weakening_constraints(self):
        unsupported = {
            "type": "object",
            "properties": {
                "email": {"type": "string", "format": "email"},
            },
            "required": ["email"],
        }
        oversized = {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "description": "x" * (17 * 1024),
                },
            },
            "required": ["value"],
        }
        unprojectable_root = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "minProperties": 1,
        }

        with self.assertRaisesRegex(ValueError, "silently weaken"):
            _declared_result_field_schema(unsupported)
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            _declared_result_field_schema(oversized)
        with self.assertRaisesRegex(ValueError, "losslessly projected"):
            _declared_result_field_schema(unprojectable_root)

        too_deep: dict = {"type": "string"}
        for _ in range(40):
            too_deep = {"anyOf": [too_deep]}
        deep_schema = {
            "type": "object",
            "properties": {"value": too_deep},
            "required": ["value"],
        }
        with self.assertRaisesRegex(ValueError, "bounded depth limit"):
            _declared_result_field_schema(deep_schema)

    def test_formal_boolean_result_schemas_are_equivalent_or_explicit_errors(self):
        unconstrained = {
            "type": "object",
            "properties": {"value": True},
            "required": ["value"],
        }
        impossible = {
            "type": "object",
            "properties": {"value": False},
            "required": ["value"],
        }

        self.assertEqual(
            {"value": {}},
            _declared_result_field_schema(unconstrained),
        )
        with self.assertRaisesRegex(ValueError, "boolean false"):
            _declared_result_field_schema(impossible)

        # List-shaped extraction contracts can carry an explicit ``schema``
        # member too. Falsy schemas must not fall back to the descriptor object
        # and become an invented ``type: object`` constraint.
        self.assertEqual(
            {"value": {}},
            _declared_result_field_schema([{"field": "value", "schema": {}}]),
        )
        self.assertEqual(
            {"value": {}},
            _declared_result_field_schema([{"field": "value", "schema": True}]),
        )
        with self.assertRaisesRegex(ValueError, "boolean false"):
            _declared_result_field_schema([
                {"field": "value", "schema": False},
            ])

    def test_declared_result_schema_normalizes_yaml_shapes_without_partial_keys(self):
        output_format = {
            "metadata": {"source": "path or URL", "date": "ISO 8601"},
            "rows": [{"id": "example", "value": 1}],
            "title": "human-readable title",
            "count": 3,
            "ratio": 0.5,
            "enabled": True,
            "optional": None,
            "score": {"type": "number", "minimum": 0},
        }

        fields = _declared_result_field_names(output_format)
        compiled = _declared_result_field_schema(output_format)

        self.assertEqual(set(fields), set(compiled))
        self.assertEqual({"type": "object"}, compiled["metadata"])
        self.assertEqual({"type": "array"}, compiled["rows"])
        self.assertEqual({"type": "string"}, compiled["title"])
        self.assertEqual({"type": "integer"}, compiled["count"])
        self.assertEqual({"type": "number"}, compiled["ratio"])
        self.assertEqual({"type": "boolean"}, compiled["enabled"])
        self.assertEqual({}, compiled["optional"])
        self.assertEqual(
            {"type": "number", "minimum": 0},
            compiled["score"],
        )

    def test_declared_result_schema_distinguishes_placeholders_from_json_schema_null(self):
        # Pure bootstrap extraction lists declare required field names but no
        # value shape. Each field therefore remains finite JSON, unconstrained.
        self.assertEqual(
            {"pmids": {}, "scores": {}},
            _declared_result_field_schema(["pmids", "scores"]),
        )

        # YAML null in an example-shaped mapping is also a placeholder. Other
        # example values continue to provide their safe coarse type.
        self.assertEqual(
            {"pmids": {}, "count": {"type": "integer"}},
            _declared_result_field_schema({"pmids": None, "count": 0}),
        )

        # An explicit formal JSON Schema null declaration remains authoritative.
        formal_schema = {
            "type": "object",
            "properties": {
                "nothing": {"type": "null"},
            },
            "required": ["nothing"],
        }
        self.assertEqual(
            {"nothing": {"type": "null"}},
            _declared_result_field_schema(formal_schema),
        )

    def test_real_v23_bootstrap_field_lists_are_required_but_unconstrained(self):
        archive_path = (
            Path(__file__).resolve().parents[2]
            / "skills_and_refs"
            / "xClinicalTrial-Design-V2.3.zip"
        )
        if not archive_path.is_file():
            self.skipTest("repository V2.3 reference archive is not packaged")

        from skills.loader import load_skill_content

        with tempfile.TemporaryDirectory() as temp_dir:
            extraction_root = Path(temp_dir)
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    member_path = PurePosixPath(member.filename)
                    self.assertFalse(member_path.is_absolute(), member.filename)
                    self.assertNotIn("..", member_path.parts, member.filename)
                archive.extractall(extraction_root)
            skill_root = extraction_root / "trial-artifs-sim"
            loaded = load_skill_content(
                skill_root / "SKILL.md",
                skill_dir=str(skill_root),
                session_id="v23-bootstrap-schema-regression",
            )
            execution = loaded.get("execution_contract") or {}
            sources = (
                (execution.get("knowledge_bootstrap") or {}).get("sources")
                or []
            )

        field_sources = [
            source for source in sources if source.get("extract_fields")
        ]
        self.assertGreaterEqual(len(field_sources), 1)
        for source in field_sources:
            with self.subTest(source=source.get("id")):
                fields = _declared_result_field_names(source["extract_fields"])
                compiled = _declared_result_field_schema(
                    source["extract_fields"]
                )
                self.assertEqual(set(fields), set(compiled))
                self.assertTrue(all(schema == {} for schema in compiled.values()))

    def test_image_prompt_does_not_require_skill_discovery_for_simple_qa(self):
        self.assertIn("Do not call skills_list or skill_view merely because an image exists", IMAGE_SKILL_MCP_GUIDANCE)
        self.assertIn("simple OCR", SESSION_SKILL_USAGE_GUIDANCE)
        self.assertIn("Answer those requests directly", SESSION_SKILL_USAGE_GUIDANCE)

    def test_direct_chat_tool_exposure_is_deterministic_and_least_privilege(self):
        available = [
            "web_search", "web_extract", "execute_code", "run_skill_python",
            "read_file", "search_files", "write_file", "patch_file",
            "merge_files", "skills_list", "skill_view", "skill_manage",
            "delegate_task", "image_generate", "mcp_server_list",
            "mcp_server_status", "session_search", "sessions_history",
            "sessions_send", "create_goal", "memory",
        ]
        cases = (
            ("天空为什么通常是蓝色？", set(), "none"),
            ("解释 execute_code 为什么刚才被调用", set(), "none"),
            ("Why did you call execute_code?", set(), "none"),
            ("How do I call execute_code?", set(), "none"),
            (
                "为什么一次简单的问题就查询了这么多遍，是判断不出来用agent还是直接chat吗",
                set(),
                "none",
            ),
            ("Why did you search the web so many times?", set(), "none"),
            ("为什么你修改了 README.md？", set(), "none"),
            ("Why did you modify README.md?", set(), "none"),
            ("为什么你运行了 Python 脚本？", set(), "none"),
            ("Why did you run a Python script?", set(), "none"),
            ("不要调用 web_search", set(), "none"),
            ("不要搜索，直接解释这个概念", set(), "none"),
            ("MCP 是什么？", set(), "none"),
            ("不要调用 MCP 连接器", set(), "none"),
            ("不要生成图片", set(), "none"),
            ("不要使用子代理", set(), "none"),
            ("搜索算法是什么？", set(), "none"),
            ("查询数据库是什么意思？", set(), "none"),
            ("Show me what a file descriptor is", set(), "none"),
            (
                "请搜索最新的 GLM 版本",
                {"web_search"},
                "none",
            ),
            ("搜索最新 file format RFC", {"web_search"}, "none"),
            ("搜索工作区文件", {"search_files"}, "none"),
            ("帮我查询为什么天空是蓝色的", {"web_search"}, "none"),
            ("web_search 帮我确认版本", {"web_search"}, "none"),
            (
                "请用 Python 运行代码计算这组数据",
                {"execute_code"},
                "none",
            ),
            (
                "读取 workspace/input.csv 并分析",
                {"read_file"},
                "none",
            ),
            ("create app.py with a hello-world program", {"write_file"}, "none"),
            ("修改 README.md 里的安装说明", {"read_file", "patch_file"}, "none"),
            ("画一只猫", {"image_generate"}, "none"),
            ("查看之前的对话历史", {"sessions_history"}, "none"),
            ("记住我喜欢蓝色", {"memory"}, "none"),
            ("创建一个目标：完成测试", {"create_goal"}, "none"),
            ("请调用 MCP 连接器", set(), "catalog"),
        )
        for text, expected, expected_mcp_policy in cases:
            with self.subTest(text=text):
                exposure = _direct_chat_tool_exposure(text, available)
                self.assertEqual(expected, set(exposure.tools))
                self.assertEqual(expected_mcp_policy, exposure.mcp_policy)

    def test_direct_chat_read_actions_never_gain_mutation_tools(self):
        available = [
            "read_file", "search_files", "write_file", "patch_file", "merge_files",
            "skills_list", "skill_view", "skill_manage",
            "sessions_history", "sessions_send",
        ]
        file_read = _direct_chat_tool_exposure("读取 workspace/input.csv", available)
        self.assertEqual(("read_file",), file_read.tools)
        skill_read = _direct_chat_tool_exposure("列出可用 Skills", available)
        self.assertEqual({"skills_list", "skill_view"}, set(skill_read.tools))
        history = _direct_chat_tool_exposure("查看对话历史", available)
        self.assertEqual(("sessions_history",), history.tools)

    def test_explicit_disabled_capability_is_reported_missing(self):
        exposure = _direct_chat_tool_exposure(
            "请搜索最新版本",
            ["read_file"],
        )
        self.assertEqual((), exposure.tools)
        self.assertIn("explicit_web_search", exposure.missing_requirements)


class SessionSkillRelevanceRunStreamTests(unittest.IsolatedAsyncioTestCase):
    provider = {
        "id": "mock-skill-relevance",
        "base_url": "http://model.invalid/v1",
        "api_model": "mock-skill-relevance",
        "api_key": "EMPTY",
        "protocol": "openai",
        "provider": "mock",
        "context_length": 64_000,
        "is_multimodal": False,
    }
    available_tools = [
        "skill_view",
        "skills_list",
        "run_skill_python",
        "run_skill_script",
        "run_declared_command",
        "skill_http_get",
        "delegate_task",
        "write_file",
        "execute_code",
    ]

    async def _run(
        self,
        request: str,
        records: list[dict],
        *,
        enabled_user_skills: list[str] | None = None,
        runnable_scripts: dict[str, tuple[tuple[str, str], ...]] | None = None,
    ):
        responses = [[
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "bounded response"},
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            }),
            "data: [DONE]",
        ]]
        request_bodies: list[dict] = []
        dispatches: list[tuple[str, dict]] = []

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
                return FakeResponse(responses.pop(0))

        by_name = {str(record["name"]): record for record in records}

        def loaded_package(path, **kwargs):
            path_text = str(path)
            record = next(
                (
                    item for name, item in by_name.items()
                    if name in path_text
                ),
                {},
            )
            return {
                "name": record.get("name"),
                "description": record.get("description", ""),
                "linked_files": {},
                "workflow_contract": None,
                "package_diagnostics": {
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                },
            }

        async def fake_dispatch(name, args, *, context):
            dispatches.append((name, dict(args)))
            record = by_name.get(str(args.get("name") or ""), {})
            return json.dumps({
                "name": record.get("name"),
                "description": record.get("description", ""),
                "content": "Follow the selected portable instructions.",
                "linked_files": {},
                "workflow_contract": None,
                "package_diagnostics": {
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                },
            })

        schemas = [{
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        } for name in self.available_tools]

        def schemas_for(names):
            allowed = set(names or [])
            return [
                schema for schema in schemas
                if schema["function"]["name"] in allowed
            ]

        with tempfile.TemporaryDirectory() as temp_dir:
            normalized_records = [
                {
                    **record,
                    "path": str(
                        Path(temp_dir) / str(record["name"]) / "SKILL.md"
                    ),
                    "skill_dir": str(
                        Path(temp_dir) / str(record["name"])
                    ),
                    "scope": str(record.get("scope") or "session"),
                }
                for record in records
            ]
            by_name = {
                str(record["name"]): record for record in normalized_records
            }
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", fake_dispatch),
                patch("agent_loop.get_schemas", side_effect=schemas_for),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("agent_loop.settings.complex_report_max_iterations", 1),
                patch("skills.loader.load_skill_content", side_effect=loaded_package),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=normalized_records,
                ),
                patch(
                    "skills.scanner.skill_runnable_script_resources",
                    side_effect=lambda name, *_args, **_kwargs: (
                        (runnable_scripts or {}).get(name, ())
                    ),
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-skill-relevance",
                        [{"role": "user", "content": request}],
                        self.available_tools,
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-skill-relevance",
                        session_id="s-skill-relevance",
                        enabled_user_skills=enabled_user_skills,
                        max_iterations=1,
                    )
                ]
        return request_bodies, dispatches, events

    async def test_high_confidence_description_match_auto_reads_only_selected_main(self):
        records = [
            {
                "name": "launch-readiness",
                "description": (
                    "Assess satellite launch telemetry, orbital safety, and "
                    "weather windows."
                ),
            },
            {
                "name": "collection-reconciler",
                "description": (
                    "Reconcile museum accession, provenance, and conservation records."
                ),
            },
        ]
        bodies, dispatches, events = await self._run(
            "Create a comprehensive satellite readiness report using telemetry "
            "and weather windows.",
            records,
        )

        self.assertEqual(
            [("skill_view", {"name": "launch-readiness", "file_path": ""})],
            dispatches,
        )
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        self.assertEqual(
            "skill_relevance_inspection",
            started["payload"]["tool_exposure_mode"],
        )
        self.assertEqual(1, started["payload"]["effective_tool_count"])
        self.assertEqual(
            ["launch-readiness"],
            started["payload"]["session_skill_relevance"]["selected_skills"],
        )
        self.assertEqual(1, len(bodies))
        self.assertNotEqual("required", bodies[0].get("tool_choice"))

    async def test_high_confidence_ordinary_request_uses_one_instruction_skill(self):
        records = [
            {
                "name": "collection-reconciler",
                "description": (
                    "Reconcile museum accession, provenance, and conservation records."
                ),
            },
            {
                "name": "launch-readiness",
                "description": (
                    "Assess satellite launch telemetry, orbital safety, and weather windows."
                ),
            },
        ]
        bodies, dispatches, events = await self._run(
            "How should I reconcile museum accession and provenance records?",
            records,
        )

        self.assertEqual(
            [("skill_view", {"name": "collection-reconciler", "file_path": ""})],
            dispatches,
        )
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        self.assertEqual(
            "relevant_skill_request",
            started["payload"]["skill_workflow_activation"],
        )
        self.assertEqual("skill_workflow", started["payload"]["execution_mode"])
        self.assertEqual(
            "skill_relevance_inspection",
            started["payload"]["tool_exposure_mode"],
        )
        self.assertEqual(1, len(bodies))
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})

    async def test_enabled_user_skill_requires_capability_plan_before_runner(self):
        records = [{
            "name": "accession-normalizer",
            "description": (
                "Normalize museum accession identifiers and provenance records."
            ),
            "scope": "user",
        }]
        bodies, dispatches, events = await self._run(
            "How should I normalize museum accession identifiers and provenance records?",
            records,
            enabled_user_skills=["accession-normalizer"],
            runnable_scripts={
                "accession-normalizer": (("scripts/normalize.sh", "a" * 64),),
            },
        )

        self.assertEqual(
            [("skill_view", {"name": "accession-normalizer", "file_path": ""})],
            dispatches,
        )
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        self.assertEqual(
            ["accession-normalizer"],
            started["payload"]["session_skill_relevance"]["selected_skills"],
        )
        exposed = {
            tool["function"]["name"]
            for tool in bodies[0].get("tools") or []
        }
        self.assertEqual({"skill_view"}, exposed)
        self.assertNotIn("run_skill_script", exposed)

    async def test_ambiguous_description_match_exposes_no_skill_authority(self):
        records = [
            {
                "name": "inventory-alpha",
                "description": "Analyze tabular inventory records for a report.",
            },
            {
                "name": "inventory-beta",
                "description": "Review tabular inventory records for a report.",
            },
        ]
        bodies, dispatches, events = await self._run(
            "How should I analyze tabular inventory records?",
            records,
        )

        self.assertEqual([], dispatches)
        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        self.assertEqual(
            "direct_none",
            started["payload"]["tool_exposure_mode"],
        )
        self.assertEqual(
            "semantic_model_pending",
            started["payload"]["session_skill_relevance"]["reason"],
        )
        exposed = {
            tool["function"]["name"]
            for tool in bodies[0].get("tools") or []
        }
        self.assertFalse(exposed.intersection({
            "skill_view",
            "skills_list",
            "run_skill_python",
            "run_skill_script",
            "run_declared_command",
            "skill_http_get",
        }))
        self.assertIn("select_session_skill", exposed)


class SimpleChatStopLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_skill_simple_chat_exposes_no_tools_or_mcp(self):
        responses = [[
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "图片中是一张简单示意图。"},
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            }),
            "data: [DONE]",
        ]]
        request_bodies = []

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
                return FakeResponse(responses.pop(0))

        dispatch_mock = AsyncMock(return_value=json.dumps({"status": "ok"}))
        schemas_mock = Mock(return_value=[])
        prompt_mock = Mock(return_value="system")
        provider = {
            "id": "mock-multimodal",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-multimodal",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": True,
        }
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图片是什么？"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"},
                },
            ],
        }]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", dispatch_mock),
                patch("agent_loop.get_schemas", schemas_mock),
                patch("agent_loop.build_system_prompt", prompt_mock),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch(
                    "skills.scanner.find_all_skills",
                    return_value=[{
                        "name": "image-question-workflow",
                        "description": (
                            "Answer image questions, identify pictures, and describe diagrams."
                        ),
                        "scope": "session",
                    }],
                ),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-multimodal",
                        messages,
                        ["skill_view", "web_search"],
                        provider_override=provider,
                        allow_session_mcp=False,
                        user_id="u-simple-boundary",
                        session_id="s-simple-boundary",
                        max_iterations=8,
                    )
                ]

        self.assertEqual(len(request_bodies), 1)
        self.assertFalse(responses)
        self.assertNotIn("tools", request_bodies[0])
        dispatch_mock.assert_not_awaited()
        schemas_mock.assert_not_called()
        self.assertEqual([], prompt_mock.call_args.kwargs["enabled_tools"])
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "stop"})
        started = next(
            event for event in events
            if event.get("event_type") == "run.started"
        )
        self.assertEqual(started["payload"]["execution_mode"], "direct_chat")
        self.assertEqual(started["payload"]["skill_workflow_activation"], "inactive")
        self.assertEqual(started["payload"]["tool_exposure_mode"], "direct_none")
        self.assertEqual(started["payload"]["effective_tool_count"], 0)
        self.assertFalse(started["payload"]["session_mcp_enabled"])
        progress = "\n".join(
            str(event.get("msg") or "")
            for event in events
            if event.get("type") == "tool_progress"
        )
        self.assertNotIn("Advancing declared session skill workflow", progress)
        self.assertNotIn("Recovering failed tool step", progress)


class WebExtractFallbackSignalTests(unittest.IsolatedAsyncioTestCase):
    async def _extract_with_response(self, status_code, text):
        class Response:
            def __init__(self):
                self.status_code = status_code
                self.text = text

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url):
                return Response()

        with patch("tools.web_extract.httpx.AsyncClient", Client):
            return await web_extract("https://example.test/article")

    async def test_http_202_recommends_browser_fallback(self):
        result = json.loads(await self._extract_with_response(202, "pending"))

        self.assertEqual("error", result["status"])
        self.assertEqual(202, result["http_status"])
        self.assertEqual("dynamic_page_pending", result["failure_kind"])
        self.assertTrue(result["browser_fallback_recommended"])
        self.assertEqual("https://example.test/article", result["url"])

    async def test_dynamic_javascript_shell_recommends_browser_fallback(self):
        result = json.loads(await self._extract_with_response(
            200,
            "<html><body><div id='root'></div><script>boot()</script></body></html>",
        ))

        self.assertEqual("dynamic_page_shell", result["failure_kind"])
        self.assertTrue(result["browser_fallback_recommended"])

    async def test_access_and_rate_failures_never_recommend_browser(self):
        for status, failure_kind in ((403, "access_denied"), (429, "rate_limited")):
            with self.subTest(status=status):
                result = json.loads(
                    await self._extract_with_response(status, "blocked")
                )
                self.assertEqual(failure_kind, result["failure_kind"])
                self.assertFalse(result["browser_fallback_recommended"])

    async def test_empty_static_response_does_not_recommend_browser(self):
        result = json.loads(await self._extract_with_response(200, ""))

        self.assertEqual("empty_response", result["failure_kind"])
        self.assertFalse(result["browser_fallback_recommended"])


class DirectRequiredToolGateTests(unittest.IsolatedAsyncioTestCase):
    provider = {
        "id": "mock-direct-tool",
        "base_url": "http://model.invalid/v1",
        "api_model": "mock-direct-tool",
        "api_key": "EMPTY",
        "protocol": "openai",
        "provider": "mock",
        "context_length": 64_000,
        "is_multimodal": False,
    }
    web_schema = [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "search",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }]
    url_read_schemas = [
        {
            "type": "function",
            "function": {
                "name": "web_extract",
                "description": "extract",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_navigate",
                "description": "navigate",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_snapshot",
                "description": "snapshot",
                "parameters": {
                    "type": "object",
                    "properties": {"full": {"type": "boolean"}},
                },
            },
        },
    ]

    @staticmethod
    def _tool_turn(call_id, name, arguments):
        return [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": call_id,
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            }),
            "data: [DONE]",
        ]

    @staticmethod
    def _stop_turn(content):
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

    async def _run_with_responses(
        self,
        responses,
        *,
        tools=None,
        dispatch_result=None,
        dispatch_side_effect=None,
        schemas=None,
        user_text="请搜索最新 GLM 版本",
        max_iterations=4,
        debug_trace=False,
    ):
        request_bodies = []

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
                return FakeResponse(responses.pop(0))

        dispatch_mock = AsyncMock(
            side_effect=dispatch_side_effect,
            return_value=dispatch_result or json.dumps({"status": "ok"}),
        )
        schema_catalog = schemas or self.web_schema

        def schemas_for(names):
            selected = set(names)
            return [
                schema for schema in schema_catalog
                if schema["function"]["name"] in selected
            ]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", dispatch_mock),
                patch("agent_loop.get_schemas", side_effect=schemas_for),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("agent_loop.settings.agent_debug_trace", debug_trace),
                patch("skills.scanner.find_all_skills", return_value=[]),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-direct-tool",
                        [{"role": "user", "content": user_text}],
                        tools or ["web_search"],
                        provider_override=self.provider,
                        allow_session_mcp=False,
                        user_id="u-direct-required",
                        session_id="s-direct-required",
                        max_iterations=max_iterations,
                    )
                ]
        return request_bodies, dispatch_mock, events

    async def test_static_url_read_stays_on_web_extract_only(self):
        target_url = "https://example.test/article"
        bodies, dispatch_mock, events = await self._run_with_responses(
            [
                self._tool_turn(
                    "extract-static", "web_extract", {"url": target_url}
                ),
                self._stop_turn("已根据静态页面正文完成总结。"),
            ],
            tools=[
                "web_extract", "browser_navigate", "browser_snapshot"
            ],
            dispatch_result="Static article body",
            schemas=self.url_read_schemas,
            user_text=f"请读取并总结 {target_url}",
        )

        self.assertEqual(2, len(bodies))
        self.assertEqual(
            {"web_extract"},
            {
                item["function"]["name"]
                for item in bodies[0].get("tools") or []
            },
        )
        self.assertNotIn("tools", bodies[1])
        dispatch_mock.assert_awaited_once_with(
            "web_extract", {"url": target_url}, context=ANY
        )
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_dynamic_url_read_auto_dispatches_same_url_browser_snapshot(self):
        target_url = "https://example.test/dynamic?record=1"
        extract_error = json.dumps({
            "status": "error",
            "url": target_url,
            "http_status": 202,
            "failure_kind": "dynamic_page_pending",
            "browser_fallback_recommended": True,
            "error": f"Failed to fetch {target_url}: HTTP 202",
        })

        async def dispatch_side_effect(name, args, *, context):
            if name == "web_extract":
                return extract_error
            if name == "browser_navigate":
                return json.dumps({
                    "status": "success",
                    "url": target_url,
                    "visible_text": "Rendered shell",
                })
            if name == "browser_snapshot":
                return json.dumps({
                    "status": "success",
                    "url": target_url,
                    "snapshot": "Rendered article body",
                })
            raise AssertionError(name)

        bodies, dispatch_mock, events = await self._run_with_responses(
            [
                self._tool_turn(
                    "extract-dynamic", "web_extract", {"url": target_url}
                ),
                self._stop_turn("已根据浏览器渲染后的正文完成总结。"),
            ],
            tools=[
                "web_extract", "browser_navigate", "browser_snapshot"
            ],
            dispatch_side_effect=dispatch_side_effect,
            schemas=self.url_read_schemas,
            user_text=f"请读取并总结 {target_url}",
            max_iterations=5,
            debug_trace=True,
        )

        self.assertEqual(2, len(bodies))
        self.assertEqual(
            {"web_extract"},
            {
                item["function"]["name"]
                for item in bodies[0].get("tools") or []
            },
        )
        self.assertNotIn("tools", bodies[1])
        self.assertEqual(
            ["web_extract", "browser_navigate", "browser_snapshot"],
            [call.args[0] for call in dispatch_mock.await_args_list],
        )
        self.assertEqual(
            {"url": target_url},
            dispatch_mock.await_args_list[1].args[1],
        )
        self.assertEqual(
            {"full": False},
            dispatch_mock.await_args_list[2].args[1],
        )
        fallback_events = [
            event for event in events
            if event.get("event_type")
            == "debug.direct_url.browser_fallback"
        ]
        self.assertEqual(2, len(fallback_events))
        serialized = json.dumps(fallback_events, ensure_ascii=False)
        self.assertNotIn(target_url, serialized)
        self.assertIn(hashlib.sha256(target_url.encode()).hexdigest(), serialized)
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_access_denial_and_rate_limit_never_escalate_to_browser(self):
        target_url = "https://example.test/protected"
        for status, failure_kind in ((403, "access_denied"), (429, "rate_limited")):
            with self.subTest(status=status):
                extract_error = json.dumps({
                    "status": "error",
                    "url": target_url,
                    "http_status": status,
                    "failure_kind": failure_kind,
                    "browser_fallback_recommended": False,
                    "error": f"Failed to fetch {target_url}: HTTP {status}",
                })
                bodies, dispatch_mock, events = await self._run_with_responses(
                    [
                        self._tool_turn(
                            f"extract-{status}",
                            "web_extract",
                            {"url": target_url},
                        ),
                        self._stop_turn("已报告网页访问失败。"),
                    ],
                    tools=[
                        "web_extract", "browser_navigate", "browser_snapshot"
                    ],
                    dispatch_result=extract_error,
                    schemas=self.url_read_schemas,
                    user_text=f"请读取并总结 {target_url}",
                )

                self.assertEqual(2, len(bodies))
                dispatch_mock.assert_awaited_once_with(
                    "web_extract", {"url": target_url}, context=ANY
                )
                self.assertEqual(
                    {"type": "done", "finish_reason": "stop"}, events[-1]
                )

    async def test_browser_fallback_requires_exact_tool_and_result_url_match(self):
        target_url = "https://example.test/requested"
        mismatched_url = "https://example.test/other"
        extract_error = json.dumps({
            "status": "error",
            "url": mismatched_url,
            "http_status": 202,
            "failure_kind": "dynamic_page_pending",
            "browser_fallback_recommended": True,
            "error": f"Failed to fetch {mismatched_url}: HTTP 202",
        })
        bodies, dispatch_mock, events = await self._run_with_responses(
            [
                self._tool_turn(
                    "extract-mismatch", "web_extract", {"url": target_url}
                ),
                self._stop_turn("已报告返回地址不一致，未升级浏览器。"),
            ],
            tools=[
                "web_extract", "browser_navigate", "browser_snapshot"
            ],
            dispatch_result=extract_error,
            schemas=self.url_read_schemas,
            user_text=f"请读取并总结 {target_url}",
        )

        self.assertEqual(2, len(bodies))
        dispatch_mock.assert_awaited_once_with(
            "web_extract", {"url": target_url}, context=ANY
        )
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_multiple_distinct_user_urls_disable_deterministic_fallback(self):
        first_url = "https://example.test/first"
        second_url = "https://example.test/second"
        extract_error = json.dumps({
            "status": "error",
            "url": first_url,
            "http_status": 202,
            "failure_kind": "dynamic_page_pending",
            "browser_fallback_recommended": True,
            "error": f"Failed to fetch {first_url}: HTTP 202",
        })
        bodies, dispatch_mock, events = await self._run_with_responses(
            [
                self._tool_turn(
                    "extract-first", "web_extract", {"url": first_url}
                ),
                self._stop_turn("请求包含多个地址，已停止确定性浏览器升级。"),
            ],
            tools=[
                "web_extract", "browser_navigate", "browser_snapshot"
            ],
            dispatch_result=extract_error,
            schemas=self.url_read_schemas,
            user_text=f"请读取 {first_url} 并与 {second_url} 比较",
        )

        self.assertEqual(2, len(bodies))
        dispatch_mock.assert_awaited_once_with(
            "web_extract", {"url": first_url}, context=ANY
        )
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_browser_navigation_failure_preserves_both_failures_without_snapshot(self):
        target_url = "https://example.test/dynamic"
        extract_error = json.dumps({
            "status": "error",
            "url": target_url,
            "http_status": 202,
            "failure_kind": "dynamic_page_pending",
            "browser_fallback_recommended": True,
            "error": "STATIC_PATH_HTTP_202",
        })

        async def dispatch_side_effect(name, args, *, context):
            if name == "web_extract":
                return extract_error
            if name == "browser_navigate":
                return "Browser navigate error: RENDERED_PATH_FAILED"
            raise AssertionError(name)

        bodies, dispatch_mock, events = await self._run_with_responses(
            [
                self._tool_turn(
                    "extract-browser-failure",
                    "web_extract",
                    {"url": target_url},
                ),
                self._stop_turn("静态读取与浏览器渲染均失败。"),
            ],
            tools=[
                "web_extract", "browser_navigate", "browser_snapshot"
            ],
            dispatch_side_effect=dispatch_side_effect,
            schemas=self.url_read_schemas,
            user_text=f"请读取并总结 {target_url}",
            max_iterations=5,
        )

        self.assertEqual(
            ["web_extract", "browser_navigate"],
            [call.args[0] for call in dispatch_mock.await_args_list],
        )
        synthesis_input = json.dumps(bodies[1]["messages"], ensure_ascii=False)
        self.assertIn("STATIC_PATH_HTTP_202", synthesis_input)
        self.assertIn("RENDERED_PATH_FAILED", synthesis_input)
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_provider_stop_without_explicit_tool_fails_closed_after_one_nudge(self):
        stop = lambda content: [
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
        responses = [stop("未经搜索的答案一"), stop("未经搜索的答案二")]
        bodies, dispatch_mock, events = await self._run_with_responses(responses)

        self.assertEqual(2, len(bodies))
        self.assertTrue(all(body.get("tool_choice") == "required" for body in bodies))
        dispatch_mock.assert_not_awaited()
        self.assertFalse([
            event for event in events if event.get("type") == "delta"
        ])
        self.assertEqual("error", events[-1]["type"])
        self.assertIn("requested tool action", events[-1]["msg"])

    async def test_explicit_tool_call_satisfies_gate_and_next_turn_is_auto(self):
        tool_turn = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call-search",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"latest GLM version"}',
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            }),
            "data: [DONE]",
        ]
        final_turn = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "已根据搜索结果确认。"},
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            }),
            "data: [DONE]",
        ]
        bodies, dispatch_mock, events = await self._run_with_responses(
            [tool_turn, final_turn],
            dispatch_result=json.dumps({"results": [{"title": "GLM"}]}),
        )

        self.assertEqual("required", bodies[0].get("tool_choice"))
        self.assertNotIn("tool_choice", bodies[1])
        self.assertNotIn("tools", bodies[1])
        dispatch_mock.assert_awaited_once()
        self.assertEqual("web_search", dispatch_mock.await_args.args[0])
        self.assertEqual(
            {"query": "latest GLM version"},
            dispatch_mock.await_args.args[1],
        )
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_single_direct_search_rejects_parallel_query_batch_atomically(self):
        parallel_turn = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-search-a",
                                "function": {
                                    "name": "web_search",
                                    "arguments": '{"query":"GLM release A"}',
                                },
                            },
                            {
                                "index": 1,
                                "id": "call-search-b",
                                "function": {
                                    "name": "web_search",
                                    "arguments": '{"query":"GLM release B"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            }),
            "data: [DONE]",
        ]
        single_turn = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call-search-clean",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"latest GLM version"}',
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            }),
            "data: [DONE]",
        ]
        final_turn = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "已根据一次有界查询确认。"},
                    "finish_reason": "stop",
                }],
            }),
            "data: [DONE]",
        ]

        bodies, dispatch_mock, events = await self._run_with_responses(
            [parallel_turn, single_turn, final_turn],
            dispatch_result=json.dumps({"results": [{"title": "GLM"}]}),
        )

        self.assertEqual(3, len(bodies))
        dispatch_mock.assert_awaited_once_with(
            "web_search",
            {"query": "latest GLM version"},
            context=ANY,
        )
        self.assertNotIn("tools", bodies[2])
        self.assertEqual({"type": "done", "finish_reason": "stop"}, events[-1])

    async def test_disabled_explicit_capability_stops_before_model_call(self):
        bodies, dispatch_mock, events = await self._run_with_responses(
            [],
            tools=["read_file"],
        )
        self.assertEqual([], bodies)
        dispatch_mock.assert_not_awaited()
        self.assertEqual("error", events[-1]["type"])
        self.assertIn("not enabled or available", events[-1]["msg"])


class PlaceholderDispatchPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_mcp_placeholder_is_never_dispatched_and_second_attempt_fails_run(self):
        marker_content = (
            "# Report\n\n```json\n"
            '{"_chatds_argument_omitted": "true", "chars": 5000}'
            "\n```"
        )

        def response_for(call_id):
            return [
                "data: " + json.dumps({
                    "choices": [{
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": call_id,
                                "function": {
                                    "name": "mcp_demo_write",
                                    "arguments": json.dumps({
                                        "filepath": "report.md",
                                        "content": marker_content,
                                    }),
                                },
                            }],
                        },
                        "finish_reason": None,
                    }],
                }),
                "data: " + json.dumps({
                    "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                }),
                "data: [DONE]",
            ]

        responses = [response_for("mcp-call-1"), response_for("mcp-call-2")]
        request_bodies = []

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
                return FakeResponse(responses.pop(0))

        mcp_schema = {
            "type": "function",
            "function": {
                "name": "mcp_demo_write",
                "description": "write through demo MCP",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["filepath", "content"],
                },
            },
        }
        provider = {
            "id": "mock-placeholder-preflight",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-placeholder-preflight",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": False,
        }
        mcp_dispatch = AsyncMock(return_value=json.dumps({"status": "written"}))

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.get_schemas", return_value=[]),
                patch(
                    "agent_loop._session_mcp_definitions_for_tools",
                    return_value=[mcp_schema],
                ),
                patch("agent_loop.build_system_prompt", return_value="system"),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
                patch("tools.mcp_client.dispatch_mcp_tool", mcp_dispatch),
                patch("skills.scanner.find_all_skills", return_value=[]),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-placeholder-preflight",
                        [{"role": "user", "content": "Write report.md with the MCP tool."}],
                        ["mcp_demo_write"],
                        provider_override=provider,
                        allow_session_mcp=False,
                        user_id="u-placeholder-preflight",
                        session_id="s-placeholder-preflight",
                        max_iterations=10,
                    )
                ]

        self.assertEqual(len(request_bodies), 2)
        self.assertFalse(responses)
        mcp_dispatch.assert_not_awaited()
        failed = [
            event for event in events
            if event.get("event_type") == "run.failed"
            and event.get("payload", {}).get("terminal_reason")
            == "placeholder_retry_exhausted"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["payload"]["attempts"], 2)
        self.assertEqual(failed[0]["payload"]["field"], "args.content")
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("failed closed", events[-1]["msg"])


if __name__ == "__main__":
    unittest.main()
