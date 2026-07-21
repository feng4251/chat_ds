import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import workspace_context
from agent_loop import (
    HarnessRunState,
    _runtime_artifact_verifier_payload,
    run_stream,
)


class DelegateActivationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_instruction_only_no_file_request_does_not_activate_artifact_verifier(self):
        state = HarnessRunState(
            user_id="u-instruction-only",
            session_id="s-instruction-only",
            enforce_skill_workflow=True,
            skill_workflow_activation="explicit_skill_request",
        )

        payload = _runtime_artifact_verifier_payload(
            state,
            artifact_policy_text=(
                "请使用 one-three-one-rule Skill，针对给内部 API 增加重试机制，"
                "给出简洁的 1-3-1 建议。不要联网，不要创建文件。"
            ),
            enforce_session_skill_workflow=True,
        )

        self.assertIsNone(payload)

    def test_primary_explicit_artifact_still_runs_integrity_verifier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(workspace_context, "WORKSPACE_ROOT", Path(temp_dir)):
                workspace = workspace_context.get_workspace("u-primary", "s-primary")
                report = workspace / "report.md"
                report.write_text("# Report\n\nVerified body.\n", encoding="utf-8")
                state = HarnessRunState(
                    user_id="u-primary",
                    session_id="s-primary",
                    enforce_skill_workflow=False,
                    skill_workflow_activation="inactive",
                )
                state.artifacts.append({
                    "kind": "file",
                    "path": "report.md",
                    "size_bytes": report.stat().st_size,
                })

                passed = _runtime_artifact_verifier_payload(
                    state,
                    artifact_policy_text="Write the result to report.md",
                    enforce_session_skill_workflow=False,
                )
                report.write_text("", encoding="utf-8")
                failed = _runtime_artifact_verifier_payload(
                    state,
                    artifact_policy_text="Write the result to report.md",
                    enforce_session_skill_workflow=False,
                )

        self.assertIsNotNone(passed)
        self.assertEqual(["report.md"], passed["target_artifacts"])
        self.assertFalse(passed["needs_more_work"])
        self.assertTrue(failed["needs_more_work"])
        self.assertIn("Artifact is empty", failed["reason"])

    def test_delegate_incidental_cache_does_not_activate_artifact_verifier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(workspace_context, "WORKSPACE_ROOT", Path(temp_dir)):
                workspace = workspace_context.get_workspace("u-cache", "s-cache")
                cache = workspace / "query_cache" / "response.json"
                cache.parent.mkdir(parents=True)
                cache.write_text('{"partial": true}', encoding="utf-8")
                state = HarnessRunState(
                    user_id="u-cache",
                    session_id="s-cache",
                    enforce_skill_workflow=False,
                    skill_workflow_activation="delegated_subtask",
                )
                state.artifacts.append({
                    "kind": "file",
                    "path": "query_cache/response.json",
                    "size_bytes": cache.stat().st_size,
                })
                state.tool_error_count = 3
                state.last_tool_error_at = 4
                state.last_successful_artifact_at = 1

                payload = _runtime_artifact_verifier_payload(
                    state,
                    # Parent artifact text is deliberately absent from the
                    # child verifier authority; the typed outer contract owns
                    # this evidence task.
                    artifact_policy_text="",
                    enforce_session_skill_workflow=False,
                )

        self.assertIsNone(payload)

    def test_delegate_declared_artifact_is_hard_gated_and_cache_is_out_of_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(workspace_context, "WORKSPACE_ROOT", Path(temp_dir)):
                workspace = workspace_context.get_workspace("u-art", "s-art")
                cache = workspace / "query_cache" / "response.json"
                cache.parent.mkdir(parents=True)
                cache.write_text('{"raw": true}', encoding="utf-8")
                state = HarnessRunState(
                    user_id="u-art",
                    session_id="s-art",
                    enforce_skill_workflow=False,
                    skill_workflow_activation="delegated_subtask",
                )
                state.artifacts.append({
                    "kind": "file",
                    "path": "query_cache/response.json",
                    "size_bytes": cache.stat().st_size,
                })

                missing = _runtime_artifact_verifier_payload(
                    state,
                    artifact_policy_text="",
                    enforce_session_skill_workflow=False,
                    declared_artifact_patterns=["deliverables/result.md"],
                )

                result = workspace / "deliverables" / "result.md"
                result.parent.mkdir(parents=True)
                result.write_text("# Result\n\nComplete.\n", encoding="utf-8")
                state.artifacts.append({
                    "kind": "file",
                    "path": "deliverables/result.md",
                    "size_bytes": result.stat().st_size,
                })
                passed = _runtime_artifact_verifier_payload(
                    state,
                    artifact_policy_text="",
                    enforce_session_skill_workflow=False,
                    declared_artifact_patterns=["deliverables/result.md"],
                )

        self.assertTrue(missing["needs_more_work"])
        self.assertEqual([], missing["target_artifacts"])
        self.assertIn("no artifact was produced", missing["reason"])
        self.assertFalse(passed["needs_more_work"])
        self.assertEqual(["deliverables/result.md"], passed["target_artifacts"])

    def test_active_skill_contract_is_authority_and_scopes_incidental_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(workspace_context, "WORKSPACE_ROOT", Path(temp_dir)):
                workspace = workspace_context.get_workspace("u-skill", "s-skill")
                cache = workspace / "query_cache" / "response.json"
                cache.parent.mkdir(parents=True)
                cache.write_text('{"raw": true}', encoding="utf-8")
                state = HarnessRunState(
                    user_id="u-skill",
                    session_id="s-skill",
                    skill_workflow_activation="explicit_skill_request",
                )
                state.artifacts.append({
                    "kind": "file",
                    "path": "query_cache/response.json",
                    "size_bytes": cache.stat().st_size,
                })
                state.skill_workflow_contracts["package-skill"] = {
                    "output_contract": {
                        "declared_artifacts": ["deliverable.json"],
                        "declared_file_count": 1,
                        "artifact_formats": {"deliverable.json": "json"},
                    },
                }

                missing = _runtime_artifact_verifier_payload(
                    state,
                    artifact_policy_text="Run package-skill",
                    enforce_session_skill_workflow=True,
                )

        self.assertIsNotNone(missing)
        self.assertTrue(missing["needs_more_work"])
        self.assertEqual([], missing["target_artifacts"])
        self.assertTrue(any(
            "declared_artifact_missing" in finding
            for finding in missing["findings"]
        ))

    async def test_parent_artifact_request_cannot_reclassify_delegate_as_direct_chat(self):
        response_lines = [
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "bounded worker result"},
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            }),
            "data: [DONE]",
        ]

        class FakeResponse:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aiter_lines(self):
                for line in response_lines:
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
                return FakeResponse()

        provider = {
            "id": "mock-delegate",
            "base_url": "http://model.invalid/v1",
            "api_model": "mock-delegate",
            "api_key": "EMPTY",
            "protocol": "openai",
            "provider": "mock",
            "context_length": 64_000,
            "is_multimodal": False,
        }
        messages = [{
            "role": "user",
            "content": (
                "Execute only this bounded evidence subtask. Parent context: "
                "请严格按照 healthsim-trialsim Skill 执行完整报告。"
            ),
        }]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
                patch("agent_loop.httpx.AsyncClient", FakeAsyncClient),
                patch("agent_loop.dispatch", AsyncMock()),
                patch("agent_loop.get_schemas", Mock(return_value=[])),
                patch("agent_loop.build_system_prompt", Mock(return_value="system")),
                patch("agent_loop.load_workspace_context", return_value=""),
                patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
            ):
                events = [
                    event
                    async for event in run_stream(
                        "mock-delegate",
                        messages,
                        ["read_file"],
                        user_id="u-delegate",
                        session_id="s-delegate",
                        provider_override=provider,
                        source="delegate",
                        agent_kind="delegate",
                        allow_session_mcp=False,
                        max_iterations=2,
                    )
                ]

        started = next(
            event for event in events if event.get("event_type") == "run.started"
        )
        self.assertEqual(started["payload"]["tool_exposure_mode"], "delegated_closed")
        self.assertEqual(started["payload"]["execution_mode"], "delegated_subtask")
        self.assertEqual(
            started["payload"]["skill_workflow_activation"],
            "delegated_subtask",
        )
        self.assertFalse(any(
            event.get("event_type") == "verifier.requested"
            for event in events
        ))
        self.assertTrue(any(
            event.get("event_type") == "run.completed"
            for event in events
        ))
        self.assertEqual("done", events[-1]["type"])


if __name__ == "__main__":
    unittest.main()
