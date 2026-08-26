import base64
import hashlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from chatds_browser_runtime.proxy_bridge import BridgeConfigurationError

from claude_runner.policy import (
    ClaudeEgressPolicyError,
    compile_turn_egress_policy,
)
from claude_runner.native_control import (
    build_native_updated_input,
    native_user_interaction_kind,
)
from claude_runner.artifact_stop_hook import evaluate_stop_hook
from claude_runner.native_lifecycle_hook import evaluate_lifecycle_hook
from claude_runner.native_workflow import (
    build_workflow_receipt,
    evaluate_workflow_hook,
    terminal_workflow_receipt,
)
from claude_runner.runner_entrypoint import (
    EventLedger,
    _claude_command,
    _close_stdin_after_native_result,
    _native_checkpoint_exists,
    _native_transcript_watermark,
    _native_stream_json_input,
    _materialize_bound_artifact_stop_contract,
    _materialize_bound_native_lifecycle_contract,
    _pending_plan_task_count,
    _quarantine_native_cron_state,
    _safe_controller_exception_code,
    _terminal_error,
    _turn_skill_binding,
    _validate_artifact_contracts,
    _emit_workspace_artifacts,
    _workspace_snapshot,
    _worker_environment,
)


def _config(*, resume: bool = False) -> dict:
    return {
        "native_session_id": str(uuid.uuid4()),
        "resume_from_native_session_id": str(uuid.uuid4()) if resume else None,
        "api_model": "glm-5.2",
        "context_window_tokens": 200000,
        "max_output_tokens": 86400,
        "provider_claude_base_url": "https://api.shaiengine.com",
        "native_web_tools": False,
        "prompt": "test",
        "input_attachments": [],
        "runtime_capability_contract": {
            "schema": "chatds.runtime-capabilities.v1",
            "structured_capabilities": ["renamed_lookup"],
            "public_http_read": {
                "enabled": True,
                "methods": ["GET", "HEAD"],
                "ports": [80, 443],
            },
        },
    }


class RunnerCommandContractTests(unittest.TestCase):
    def test_native_question_answers_bind_to_exact_durable_input(self):
        museum_input = {
            "questions": [{
                "question": "Which gallery should be audited?",
                "header": "Gallery",
                "multiSelect": False,
                "options": [
                    {"label": "East", "description": "Audit the east wing"},
                    {"label": "West", "description": "Audit the west wing"},
                ],
            }],
            "metadata": {"source": "renamed-domain"},
        }
        self.assertEqual(
            native_user_interaction_kind("AskUserQuestion", museum_input),
            "question",
        )
        updated = build_native_updated_input(
            tool_name="AskUserQuestion",
            native_input=museum_input,
            answers={"Which gallery should be audited?": "East"},
        )
        self.assertEqual(
            updated["answers"],
            {"Which gallery should be audited?": "East"},
        )
        self.assertEqual(updated["metadata"], museum_input["metadata"])

        with self.assertRaisesRegex(ValueError, "answers_invalid"):
            build_native_updated_input(
                tool_name="AskUserQuestion",
                native_input=museum_input,
                answers={"Which factory line should be audited?": "Line A"},
            )

        mutated = {
            "questions": [{
                "question": "Which factory line should be audited?",
                "header": "Line",
                "options": [
                    {"label": "Line A", "description": "First line"},
                    {"label": "Line A", "description": "Duplicate label"},
                ],
            }],
        }
        self.assertIsNone(
            native_user_interaction_kind("AskUserQuestion", mutated)
        )
        self.assertEqual(
            native_user_interaction_kind("ExitPlanMode", {"plan": "# Plan"}),
            "user_action",
        )

    def test_interactive_stream_input_closes_only_after_native_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            process = SimpleNamespace(stdin=io.BytesIO())
            lock = threading.Lock()
            self.assertFalse(_close_stdin_after_native_result(
                process, lock, ledger, keep_stdin_open=True,
            ))
            ledger.append_line(
                json.dumps({
                    "type": "result",
                    "subtype": "success",
                    "result": "renamed cross-domain response",
                }).encode(),
                channel="stdout",
            )
            self.assertTrue(_close_stdin_after_native_result(
                process, lock, ledger, keep_stdin_open=True,
            ))
            self.assertTrue(process.stdin.closed)
            ledger.close()

    def test_native_ledger_returns_the_exact_controller_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            first = ledger.append_line(
                json.dumps({"type": "system", "subtype": "init"}).encode(),
                channel="stdout",
            )
            second = ledger.append_event(
                {"type": "chatds.fixture"}, channel="controller"
            )
            ledger.close()
        self.assertEqual((first, second), (1, 2))

    def test_permission_presets_use_native_claude_modes_without_widening_tools(self):
        full, _ = _claude_command({**_config(), "permission_preset": "session_full"})
        writable, _ = _claude_command({**_config(), "permission_preset": "workspace_write"})
        readonly, _ = _claude_command({**_config(), "permission_preset": "read_only"})
        self.assertIn("--dangerously-skip-permissions", full)
        self.assertEqual(full[full.index("--permission-mode") + 1], "bypassPermissions")
        self.assertNotIn("--dangerously-skip-permissions", writable)
        self.assertEqual(writable[writable.index("--permission-mode") + 1], "default")
        self.assertNotIn("--dangerously-skip-permissions", readonly)
        self.assertEqual(readonly[readonly.index("--permission-mode") + 1], "plan")
        for command in (full, writable, readonly):
            self.assertEqual(command[command.index("--tools") + 1], "default")
            self.assertEqual(
                command[command.index("--permission-prompt-tool") + 1],
                "stdio",
            )

    def test_immutable_skill_resources_are_a_native_read_root_in_every_tier(self):
        for permission_preset in (
            "read_only",
            "workspace_write",
            "session_full",
        ):
            with self.subTest(permission_preset=permission_preset):
                command, _ = _claude_command({
                    **_config(),
                    "permission_preset": permission_preset,
                })
                self.assertIn("--add-dir", command)
                self.assertEqual(
                    command[command.index("--add-dir") + 1],
                    "/skill-view/plugin/skills",
                )
                self.assertNotIn("/state", command)

    def test_missing_permission_is_native_confirmation_not_full_access(self):
        command, _ = _claude_command(_config())
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertEqual(
            command[command.index("--permission-mode") + 1],
            "default",
        )
        self.assertEqual(
            command[command.index("--permission-prompt-tool") + 1],
            "stdio",
        )

    def test_fresh_session_primary_uses_native_skill_command_lowering(self):
        config = {
            **_config(),
            "prompt": "Audit the renamed museum collection",
            "turn_skill_binding": {
                "skill_name": "museum-provenance",
                "source": "fresh_session_primary",
            },
        }
        command, encoded = _claude_command(config)
        envelope = json.loads(encoded)
        self.assertEqual(
            envelope["message"]["content"][0]["text"],
            "/chatds-session-skills:museum-provenance "
            "Audit the renamed museum collection",
        )
        self.assertNotIn("--append-system-prompt", command)
        self.assertEqual(
            _turn_skill_binding(config),
            ("museum-provenance", "fresh_session_primary"),
        )

        with self.assertRaisesRegex(
            RuntimeError, "turn_skill_binding_invalid"
        ):
            _native_stream_json_input(
                {
                    **_config(resume=True),
                    "turn_skill_binding": config["turn_skill_binding"],
                },
                workspace_root=Path("/workspace"),
            )

        renamed_mutation = {
            **config,
            "turn_skill_binding": {
                "skill_name": "warehouse planner",
                "source": "fresh_session_primary",
            },
        }
        with self.assertRaisesRegex(
            RuntimeError, "turn_skill_binding_invalid"
        ):
            _turn_skill_binding(renamed_mutation)

    def test_only_static_bridge_failures_receive_durable_diagnostic_codes(self):
        self.assertEqual(
            _safe_controller_exception_code(
                BridgeConfigurationError("invalid exact egress rule")
            ),
            "egress_invalid_exact_egress_rule",
        )
        self.assertIsNone(
            _safe_controller_exception_code(RuntimeError("secret=/tmp/value"))
        )
        self.assertEqual(
            _safe_controller_exception_code(
                RuntimeError("native_task_state_invalid")
            ),
            "native_task_state_invalid",
        )

    def test_fresh_and_resumed_turns_use_transactional_native_sessions(self):
        fresh = _config()
        command, prompt = _claude_command(fresh)
        native_input = json.loads(prompt)
        self.assertEqual(native_input["type"], "user")
        self.assertEqual(native_input["message"], {
            "role": "user",
            "content": [{"type": "text", "text": "test"}],
        })
        self.assertEqual(native_input["parent_tool_use_id"], None)
        self.assertEqual(native_input["session_id"], "")
        self.assertTrue(prompt.endswith(b"\n"))
        self.assertNotIn("--bare", command)
        self.assertEqual(
            command[command.index("--input-format") + 1],
            "stream-json",
        )
        self.assertIn("--no-chrome", command)
        self.assertIn("--thinking", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(
            command[command.index("--mcp-config") + 1],
            "/skill-view/plugin/.mcp.json",
        )
        self.assertEqual(command[command.index("--tools") + 1], "default")
        self.assertNotIn("--append-system-prompt", command)
        self.assertEqual(
            command[command.index("--disallowedTools") + 1],
            "CronCreate,CronDelete,CronList,WebFetch,WebSearch",
        )
        self.assertEqual(command[command.index("--session-id") + 1], fresh["native_session_id"])
        self.assertNotIn("--resume", command)

        resumed = _config(resume=True)
        command, _ = _claude_command(resumed)
        self.assertEqual(
            command[command.index("--resume") + 1],
            resumed["resume_from_native_session_id"],
        )
        self.assertIn("--fork-session", command)
        self.assertEqual(command[command.index("--session-id") + 1], resumed["native_session_id"])
        self.assertNotIn("--append-system-prompt", command)

        native_web = {**fresh, "native_web_tools": True}
        command, _ = _claude_command(native_web)
        self.assertEqual(
            command[command.index("--disallowedTools") + 1],
            "CronCreate,CronDelete,CronList",
        )

    def test_verified_image_is_top_level_sdk_input_not_a_tool_result(self):
        payload = b"\x89PNG\r\n\x1a\nrenamed-cross-domain-image-payload"
        digest = hashlib.sha256(payload).hexdigest()
        receipt = {
            "schema": "chatds.input-attachment.v1",
            "kind": "image",
            "path": f".chatds/input-attachments/{digest}.png",
            "sha256": digest,
            "media_type": "image/png",
            "size_bytes": len(payload),
            "width": 17,
            "height": 29,
        }
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            target = workspace / receipt["path"]
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            command, prompt = _claude_command(
                {
                    **_config(),
                    "prompt": "Inspect the renamed holdout image.",
                    "input_attachments": [receipt],
                },
                workspace_root=workspace,
            )

        native_input = json.loads(prompt)
        content = native_input["message"]["content"]
        self.assertEqual([block["type"] for block in content], ["text", "image"])
        self.assertEqual(content[1]["source"]["type"], "base64")
        self.assertEqual(content[1]["source"]["media_type"], "image/png")
        self.assertEqual(
            base64.b64decode(content[1]["source"]["data"]),
            payload,
        )
        self.assertNotIn("tool_result", prompt.decode("utf-8"))
        self.assertNotIn("Read", prompt.decode("utf-8"))
        self.assertIn("--input-format", command)

    def test_extended_context_is_a_client_marker_not_an_upstream_model_id(self):
        config = {
            **_config(),
            "api_model": "renamed-model-holdout",
            "context_window_tokens": 1_000_000,
        }
        command, _ = _claude_command(config)
        self.assertEqual(
            command[command.index("--model") + 1],
            "renamed-model-holdout[1m]",
        )

        sub_million = {
            **config,
            "context_window_tokens": 303_872,
        }
        command, _ = _claude_command(sub_million)
        self.assertEqual(
            command[command.index("--model") + 1],
            "renamed-model-holdout[1m]",
        )

        baseline = {
            **config,
            "context_window_tokens": 200_000,
        }
        command, _ = _claude_command(baseline)
        self.assertEqual(
            command[command.index("--model") + 1],
            "renamed-model-holdout",
        )

    def test_missing_or_unbounded_context_fails_before_native_start(self):
        for value in (None, True, 199_999, 4_000_001):
            with self.subTest(value=value):
                config = {**_config(), "context_window_tokens": value}
                with self.assertRaisesRegex(
                    RuntimeError,
                    "model_context_window_invalid",
                ):
                    _claude_command(config)

    def test_invalid_runtime_capability_contract_fails_before_native_start(self):
        for contract in (
            None,
            {"schema": "wrong"},
            {
                "schema": "chatds.runtime-capabilities.v1",
                "structured_capabilities": ["unsafe capability"],
                "public_http_read": {
                    "enabled": True,
                    "methods": ["POST"],
                    "ports": [443],
                },
            },
        ):
            with self.subTest(contract=contract):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "runtime_capability_contract_invalid",
                ):
                    _claude_command({
                        **_config(),
                        "runtime_capability_contract": contract,
                    })

    def test_worker_environment_is_explicit_and_binds_output_and_proxy(self):
        config = _config()
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"CLAUDE_PROVIDER_API_KEY": "test-key"},
            clear=False,
        ):
            environment = _worker_environment(
                config,
                trust={"SSL_CERT_FILE": "/runtime/ca.pem"},
                proxy_url="http://127.0.0.1:12345",
                worker_tmp=Path(temporary),
            )
        self.assertEqual(environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "86400")
        self.assertEqual(environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "200000")
        self.assertEqual(environment["ANTHROPIC_BASE_URL"], "https://api.shaiengine.com")
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:12345")
        self.assertEqual(environment["SKILL_EGRESS_PROXY_URL"], environment["HTTPS_PROXY"])
        self.assertEqual(environment["DISABLE_TELEMETRY"], "1")
        self.assertNotIn("USER_TYPE", environment)
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", environment)
        self.assertNotIn("SKILL_EGRESS_POLICY_TOKEN", environment)

    def test_local_openai_catalog_binding_still_uses_anthropic_messages_base(self):
        config = {
            **_config(),
            "api_model": "AgentModel",
            "provider_claude_base_url": "http://10.10.132.2:1025",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"CLAUDE_PROVIDER_API_KEY": "EMPTY"},
            clear=False,
        ):
            environment = _worker_environment(
                config,
                trust={},
                proxy_url="http://127.0.0.1:12345",
                worker_tmp=Path(temporary),
            )
        self.assertEqual(
            environment["ANTHROPIC_BASE_URL"],
            "http://10.10.132.2:1025",
        )

    def test_only_one_successful_stdout_result_can_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.append_line(
                b'{"type":"result","subtype":"success","is_error":false}',
                channel="stderr",
            )
            self.assertFalse(ledger.saw_native_result)
            self.assertFalse(ledger.native_result_succeeded)

            ledger.append_line(
                b'{"type":"result","subtype":"success","is_error":false}',
                channel="stdout",
            )
            self.assertTrue(ledger.saw_native_result)
            self.assertTrue(ledger.native_result_succeeded)
            self.assertEqual(ledger.native_result_count, 1)

            ledger.append_line(
                b'{"type":"result","subtype":"success","is_error":false}',
                channel="stdout",
            )
            self.assertFalse(ledger.native_result_succeeded)
            self.assertEqual(ledger.native_result_count, 2)
            ledger.close()

    def test_native_error_result_is_observed_but_cannot_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.append_line(
                b'{"type":"result","subtype":"error_during_execution",'
                b'"is_error":true}',
                channel="stdout",
            )
            self.assertTrue(ledger.saw_native_result)
            self.assertFalse(ledger.native_result_succeeded)
            self.assertEqual(ledger.native_result_count, 1)
            ledger.close()

    def test_native_provider_status_is_retained_as_bounded_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.append_line(
                b'{"type":"result","subtype":"success","is_error":true,'
                b'"api_error_status":403}',
                channel="stdout",
            )
            self.assertEqual(ledger.native_api_error_status, 403)
            ledger.close()

    def test_structured_provider_failure_precedes_result_multiplicity(self):
        """A causal provider receipt must not be hidden by drain-turn results."""

        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            for result_id in (
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            ):
                ledger.append_line(json.dumps({
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "api_error_status": 429,
                    "uuid": result_id,
                    "origin": {"kind": "task-notification"},
                }).encode(), channel="stdout")
            self.assertEqual(
                _terminal_error(
                    termination_reason=None,
                    exit_code=1,
                    ledger=ledger,
                    checkpoint_ready=True,
                    egress_receipt={"exhausted": False},
                    pending_plan_task_count=0,
                    pending_native_task_count=0,
                    workflow_contract_passed=False,
                    artifact_contract_passed=False,
                ),
                "provider_http_429",
            )
            ledger.close()

    def test_exhausted_egress_receipt_beats_opaque_native_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.append_line(
                b'{"type":"result","subtype":"success","is_error":true}',
                channel="stdout",
            )
            self.assertEqual(
                _terminal_error(
                    termination_reason=None,
                    exit_code=1,
                    ledger=ledger,
                    checkpoint_ready=True,
                    egress_receipt={"exhausted": True},
                    pending_plan_task_count=0,
                    pending_native_task_count=0,
                ),
                "egress_budget_exhausted",
            )
            ledger.close()

    def test_workflow_receipt_failure_precedes_artifact_audit_at_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.append_line(
                b'{"type":"result","subtype":"success","is_error":false}',
                channel="stdout",
            )
            self.assertEqual(
                _terminal_error(
                    termination_reason=None,
                    exit_code=0,
                    ledger=ledger,
                    checkpoint_ready=True,
                    egress_receipt={"exhausted": False},
                    pending_plan_task_count=0,
                    pending_native_task_count=0,
                    workflow_contract_passed=False,
                    artifact_contract_passed=False,
                ),
                "workflow_contract_failed",
            )
            ledger.close()

    def test_native_subtask_lifecycle_and_plan_tasks_are_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = EventLedger(root / "events.jsonl")
            ledger.append_line(
                b'{"type":"system","subtype":"task_started",'
                b'"task_id":"child-1"}',
                channel="stdout",
            )
            self.assertEqual(ledger.active_native_task_count, 1)
            ledger.append_line(
                b'{"type":"system","subtype":"task_notification",'
                b'"task_id":"child-1","status":"completed"}',
                channel="stdout",
            )
            self.assertEqual(ledger.active_native_task_count, 0)
            ledger.close()

            native_session_id = str(uuid.uuid4())
            task_root = root / "tasks" / native_session_id
            task_root.mkdir(parents=True)
            (task_root / "1.json").write_text(
                json.dumps({"status": "in_progress"}),
                encoding="utf-8",
            )
            self.assertEqual(
                _pending_plan_task_count(root / "tasks", native_session_id),
                1,
            )
            success_ledger = EventLedger(root / "success-events.jsonl")
            success_ledger.append_line(
                b'{"type":"result","subtype":"success",'
                b'"is_error":false}',
                channel="stdout",
            )
            self.assertIsNone(_terminal_error(
                termination_reason=None,
                exit_code=0,
                ledger=success_ledger,
                checkpoint_ready=True,
                egress_receipt={"exhausted": False},
                pending_plan_task_count=1,
                pending_native_task_count=0,
            ))
            success_ledger.close()
            (task_root / "1.json").write_text(
                json.dumps({"status": "completed"}),
                encoding="utf-8",
            )
            self.assertEqual(
                _pending_plan_task_count(root / "tasks", native_session_id),
                0,
            )

    def test_mandatory_worker_receipts_gate_fan_in_and_allow_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            project = projects / "-workspace"
            project.mkdir(parents=True)
            native_session_id = str(uuid.uuid4())
            (project / f"{native_session_id}.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )
            subagents = project / native_session_id / "subagents"
            subagents.mkdir(parents=True)
            contract = {
                "schema": "chatds.skill-workflow-contract.v1",
                "skill_name": "warehouse-audit",
                "route_id": "full_inventory_review",
                "source_path": "orchestration/orchestrator.yaml",
                "priority": 7,
                "matched_pattern_index": 0,
                "route_sha256": "a" * 64,
                "phases": [
                    {
                        "mode": "parallel",
                        "workers": [
                            {
                                "worker_id": "stock-auditor",
                                "native_agent_type": (
                                    "chatds-session-skills:warehouse-audit:"
                                    "stock-auditor"
                                ),
                            },
                            {
                                "worker_id": "ledger-reviewer",
                                "native_agent_type": (
                                    "chatds-session-skills:warehouse-audit:"
                                    "ledger-reviewer"
                                ),
                            },
                        ],
                    },
                    {
                        "mode": "sequential",
                        "workers": [{
                            "worker_id": "signoff-reviewer",
                            "native_agent_type": (
                                "chatds-session-skills:warehouse-audit:"
                                "signoff-reviewer"
                            ),
                        }],
                    },
                ],
            }
            receipt_path = root / "workflow-receipt.json"
            ledger = EventLedger(root / "events.jsonl")
            ledger.bind_native_task_state(
                projects_root=projects,
                native_session_id=native_session_id,
            )
            ledger.bind_workflow_contract(
                contract=contract,
                receipt_path=receipt_path,
            )

            def start(task_id: str, tool_id: str, agent_type: str) -> None:
                ledger.append_line(json.dumps({
                    "type": "system",
                    "subtype": "task_started",
                    "task_id": task_id,
                    "task_type": "local_agent",
                    "tool_use_id": tool_id,
                    "session_id": native_session_id,
                    "subagent_type": agent_type,
                }).encode(), channel="stdout")

            def finish(
                task_id: str,
                tool_id: str,
                agent_type: str,
                *,
                api_error: bool = False,
            ) -> None:
                (subagents / f"agent-{task_id}.meta.json").write_text(
                    json.dumps({
                        "agentType": agent_type,
                        "description": "generic fixture",
                        "toolUseId": tool_id,
                    }),
                    encoding="utf-8",
                )
                record = {
                    "type": "assistant",
                    "agentId": task_id,
                    "sessionId": native_session_id,
                    "message": {
                        "stop_reason": (
                            "stop_sequence" if api_error else "end_turn"
                        ),
                        "content": [{
                            "type": "text",
                            "text": (
                                "API Error: disconnected"
                                if api_error else "Completed evidence review."
                            ),
                        }],
                    },
                    **(
                        {"isApiErrorMessage": True, "error": "unknown"}
                        if api_error else {}
                    ),
                }
                (subagents / f"agent-{task_id}.jsonl").write_text(
                    json.dumps(record) + "\n", encoding="utf-8"
                )
                ledger.append_line(json.dumps({
                    "type": "system",
                    "subtype": "task_updated",
                    "task_id": task_id,
                    "patch": {"status": "completed"},
                }).encode(), channel="stdout")

            stock_type = contract["phases"][0]["workers"][0][
                "native_agent_type"
            ]
            ledger_type = contract["phases"][0]["workers"][1][
                "native_agent_type"
            ]
            signoff_type = contract["phases"][1]["workers"][0][
                "native_agent_type"
            ]
            start("agent-stock", "tool-stock", stock_type)
            finish("agent-stock", "tool-stock", stock_type)
            start("agent-ledger-1", "tool-ledger-1", ledger_type)
            finish(
                "agent-ledger-1", "tool-ledger-1", ledger_type,
                api_error=True,
            )
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["frontier_index"], 0)
            self.assertEqual(
                [worker["status"] for worker in receipt["phases"][0]["workers"]],
                ["succeeded", "failed"],
            )
            self.assertEqual(
                evaluate_workflow_hook(
                    hook_input={
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Agent",
                        "tool_use_id": "later-phase",
                        "tool_input": {"subagent_type": signoff_type},
                    },
                    contract=contract,
                    receipt=receipt,
                    artifact_contracts=[{
                        "declared_final_artifact": "{NAME}_FINAL.md",
                        "declared_modular_files": ["01_*.md"],
                    }],
                )["hookSpecificOutput"]["permissionDecision"],
                "deny",
            )
            optional_before_frontier = evaluate_workflow_hook(
                hook_input={
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Agent",
                    "tool_use_id": "optional-before-frontier",
                    "tool_input": {"subagent_type": "general-purpose"},
                },
                contract=contract,
                receipt=receipt,
                artifact_contracts=[],
            )
            self.assertEqual(
                optional_before_frontier["hookSpecificOutput"]
                ["permissionDecision"],
                "deny",
            )
            self.assertIn(
                "chatds-session-skills:warehouse-audit:ledger-reviewer",
                optional_before_frontier["hookSpecificOutput"]
                ["permissionDecisionReason"],
            )
            self.assertIn(
                "Do not retry the blocked tool",
                optional_before_frontier["hookSpecificOutput"]
                ["permissionDecisionReason"],
            )
            self.assertEqual(
                evaluate_workflow_hook(
                    hook_input={
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Write",
                        "tool_use_id": "early-write",
                        "tool_input": {
                            "file_path": "/workspace/output/01_report.md"
                        },
                    },
                    contract=contract,
                    receipt=receipt,
                    artifact_contracts=[{
                        "declared_final_artifact": "{NAME}_FINAL.md",
                        "declared_modular_files": ["01_*.md"],
                    }],
                )["hookSpecificOutput"]["permissionDecision"],
                "deny",
            )
            self.assertEqual(
                evaluate_workflow_hook(
                    hook_input={
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Agent",
                        "tool_use_id": "retry",
                        "tool_input": {"subagent_type": ledger_type},
                    },
                    contract=contract,
                    receipt=receipt,
                    artifact_contracts=[],
                ),
                {},
            )

            start("agent-ledger-2", "tool-ledger-2", ledger_type)
            finish("agent-ledger-2", "tool-ledger-2", ledger_type)
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["frontier_index"], 1)
            start("agent-signoff", "tool-signoff", signoff_type)
            finish("agent-signoff", "tool-signoff", signoff_type)
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["frontier_index"], 2)
            self.assertEqual(
                terminal_workflow_receipt(contract, receipt)["status"],
                "passed",
            )
            self.assertEqual(
                evaluate_workflow_hook(
                    hook_input={
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Write",
                        "tool_use_id": "final-write",
                        "tool_input": {
                            "file_path": "/workspace/output/01_report.md"
                        },
                    },
                    contract=contract,
                    receipt=receipt,
                    artifact_contracts=[{
                        "declared_final_artifact": "{NAME}_FINAL.md",
                        "declared_modular_files": ["01_*.md"],
                    }],
                ),
                {},
            )
            ledger.close()

    def test_missing_sequential_worker_is_a_machine_terminal_failure(self):
        contract = {
            "schema": "chatds.skill-workflow-contract.v1",
            "skill_name": "museum-provenance-renamed",
            "route_id": "collection_chain",
            "source_path": "orchestration/orchestrator.yml",
            "priority": 4,
            "matched_pattern_index": 0,
            "route_sha256": "b" * 64,
            "phases": [{
                "mode": "sequential",
                "workers": [{
                    "worker_id": "curator-signoff",
                    "native_agent_type": (
                        "chatds-session-skills:museum-provenance-renamed:"
                        "curator-signoff"
                    ),
                }],
            }],
        }
        receipt = {
            "schema": "chatds.native-workflow-receipt.v1",
            "skill_name": contract["skill_name"],
            "route_id": contract["route_id"],
            "route_sha256": contract["route_sha256"],
            "status": "pending",
            "frontier_index": 0,
            "phases": [{
                "mode": "sequential",
                "status": "pending",
                "workers": [{
                    **contract["phases"][0]["workers"][0],
                    "status": "missing",
                    "attempt_count": 0,
                    "terminal_by_status": {},
                }],
            }],
            "violations": [],
        }
        terminal = terminal_workflow_receipt(contract, receipt)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(
            terminal["findings"][0]["code"], "workflow_worker_missing"
        )

    def test_fresh_native_session_binds_before_projects_tree_exists(self):
        contract = {
            "schema": "chatds.skill-workflow-contract.v1",
            "skill_name": "museum-audit-renamed",
            "route_id": "new_collection_review",
            "source_path": "orchestration/orchestrator.yml",
            "priority": 2,
            "matched_pattern_index": 0,
            "route_sha256": "e" * 64,
            "phases": [{
                "mode": "sequential",
                "workers": [{
                    "worker_id": "curator",
                    "native_agent_type": (
                        "chatds-session-skills:museum-audit-renamed:curator"
                    ),
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "state" / "home"
            home.mkdir(parents=True)
            projects = home / ".claude" / "projects"
            self.assertFalse(projects.exists())
            ledger = EventLedger(root / "events.jsonl")
            ledger.bind_native_task_state(
                projects_root=projects,
                native_session_id=str(uuid.uuid4()),
            )
            receipt_path = root / "workflow-receipt.json"
            ledger.bind_workflow_contract(
                contract=contract,
                receipt_path=receipt_path,
            )
            self.assertEqual(
                json.loads(receipt_path.read_text())["frontier_index"], 0
            )
            # Claude Code creates the transcript tree on its first write.
            self.assertFalse(projects.exists())
            ledger.close()

    def test_terminal_notification_waits_for_durable_subagent_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            project = projects / "-workspace"
            project.mkdir(parents=True)
            native_session_id = str(uuid.uuid4())
            (project / f"{native_session_id}.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )
            subagents = project / native_session_id / "subagents"
            subagents.mkdir(parents=True)
            native_agent_type = (
                "chatds-session-skills:satellite-review:telemetry-reader"
            )
            contract = {
                "schema": "chatds.skill-workflow-contract.v1",
                "skill_name": "satellite-review",
                "route_id": "telemetry_chain",
                "source_path": "orchestration/orchestrator.yaml",
                "priority": 5,
                "matched_pattern_index": 0,
                "route_sha256": "f" * 64,
                "phases": [{
                    "mode": "sequential",
                    "workers": [{
                        "worker_id": "telemetry-reader",
                        "native_agent_type": native_agent_type,
                    }],
                }],
            }
            receipt_path = root / "workflow-receipt.json"
            ledger = EventLedger(root / "events.jsonl")
            ledger.bind_native_task_state(
                projects_root=projects,
                native_session_id=native_session_id,
            )
            ledger.bind_workflow_contract(
                contract=contract,
                receipt_path=receipt_path,
            )
            ledger.append_line(json.dumps({
                "type": "system",
                "subtype": "task_started",
                "task_id": "telemetry-task",
                "task_type": "local_agent",
                "tool_use_id": "telemetry-tool",
                "session_id": native_session_id,
                "subagent_type": native_agent_type,
            }).encode(), channel="stdout")

            # Claude Code deliberately publishes the task status before its
            # asynchronous transcript write queue is guaranteed durable.
            ledger.append_line(json.dumps({
                "type": "system",
                "subtype": "task_notification",
                "task_id": "telemetry-task",
                "status": "completed",
            }).encode(), channel="stdout")
            pending = json.loads(receipt_path.read_text())
            self.assertEqual(
                pending["phases"][0]["workers"][0]["status"], "running"
            )

            (subagents / "agent-telemetry-task.meta.json").write_text(
                json.dumps({
                    "agentType": native_agent_type,
                    "description": "generic satellite fixture",
                    "toolUseId": "telemetry-tool",
                }),
                encoding="utf-8",
            )
            (subagents / "agent-telemetry-task.jsonl").write_text(
                json.dumps({
                    "type": "assistant",
                    "agentId": "telemetry-task",
                    "sessionId": native_session_id,
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{
                            "type": "text",
                            "text": "TELEMETRY_RECEIPT=passed",
                        }],
                    },
                }) + "\n",
                encoding="utf-8",
            )
            ledger.append_line(
                b'{"type":"system","subtype":"status","status":"ready"}',
                channel="stdout",
            )
            settled = json.loads(receipt_path.read_text())
            self.assertEqual(settled["status"], "passed")
            ledger.close()

    def test_native_cron_file_is_archived_out_of_active_load_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".claude"
            root.mkdir()
            source = root / "scheduled_tasks.json"
            source.write_text(
                json.dumps({"tasks": [{"prompt": "stale unrelated task"}]}),
                encoding="utf-8",
            )
            run_id = "d" * 32

            self.assertTrue(_quarantine_native_cron_state(root, run_id))
            self.assertFalse(source.exists())
            archived = root / "chatds-native-cron-archive" / f"{run_id}.json"
            self.assertTrue(archived.is_file())
            self.assertFalse(_quarantine_native_cron_state(root, "e" * 32))

    def test_task_output_terminal_receipt_closes_missing_notification(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.append_line(
                b'{"type":"system","subtype":"task_started",'
                b'"task_id":"bfixture01","task_type":"local_bash"}',
                channel="stdout",
            )
            ledger.append_line(
                b'{"type":"assistant","message":{"content":[{'
                b'"type":"tool_use","id":"tool-output-1",'
                b'"name":"TaskOutput","input":{"task_id":"bfixture01"}}]}}',
                channel="stdout",
            )
            ledger.append_line(json.dumps({
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-output-1",
                    "is_error": False,
                    "content": (
                        "<retrieval_status>success</retrieval_status>\n"
                        "<task_id>bfixture01</task_id>\n"
                        "<task_type>local_bash</task_type>\n"
                        "<status>completed</status>\n"
                        "<exit_code>0</exit_code>"
                    ),
                }]},
            }).encode(), channel="stdout")
            self.assertEqual(ledger.active_native_task_count, 0)
            self.assertEqual(
                ledger.native_task_summary["reconciled_by"]["task_output"],
                1,
            )
            ledger.close()

    def test_schedule_control_receipt_becomes_controller_pending_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            arguments = {
                "name": "Cross-domain equipment monitor",
                "prompt": "Report both selected equipment readings.",
                "schedule": "*/10 13-14 12 8 *",
                "timezone": "Asia/Shanghai",
                "max_runs": 12,
                "expires_at": "2099-08-12T15:00:00+08:00",
            }
            from claude_runner.mcp_schedule_control import _accepted_receipt
            ledger.append_line(json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "schedule-tool-1",
                    "name": "mcp__chatds-schedule__schedule_create",
                    "input": arguments,
                }]},
            }).encode(), channel="stdout")
            ledger.append_line(json.dumps({
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "schedule-tool-1",
                    "is_error": False,
                    "content": _accepted_receipt(arguments),
                }]},
            }).encode(), channel="stdout")

            self.assertEqual(len(ledger.pending_control_writes), 1)
            write = ledger.pending_control_writes[0]
            self.assertEqual(write["operation"], "create")
            self.assertEqual(write["request"]["max_runs"], 12)
            ledger.close()

    def test_schedule_receipt_uses_same_compiled_names_at_both_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            aliases = {
                "mcp__chatds-market-data__market_quote": "market_quote",
            }
            arguments = {
                "name": "Renamed public metric monitor",
                "prompt": "Report the selected public metric.",
                "schedule": "every 5m",
                "timezone": "UTC",
                "platform_capabilities": [
                    "mcp__chatds-market-data__market_quote",
                ],
            }
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.bind_schedule_capability_aliases(aliases)
            ledger.append_line(json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "schedule-tool-renamed",
                    "name": "mcp__chatds-schedule__schedule_create",
                    "input": arguments,
                }]},
            }).encode(), channel="stdout")
            from claude_runner.mcp_schedule_control import (
                SCHEDULE_CAPABILITY_ALIASES_ENV,
                _accepted_receipt,
            )
            with patch.dict(os.environ, {
                SCHEDULE_CAPABILITY_ALIASES_ENV: json.dumps(aliases),
            }):
                receipt = _accepted_receipt(arguments)
            ledger.append_line(json.dumps({
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "schedule-tool-renamed",
                    "is_error": False,
                    "content": receipt,
                }]},
            }).encode(), channel="stdout")
            self.assertEqual(
                ledger.pending_control_writes[0]["request"][
                    "platform_capabilities"
                ],
                ["market_quote"],
            )
            ledger.close()

    def test_rejected_schedule_never_becomes_a_pending_control_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            arguments = {
                "name": "Renamed inventory monitor",
                "prompt": "Report the selected inventory reading.",
                "schedule": "*/2 * * * *",
                "timezone": "UTC",
                "max_runs": 5,
                "expires_at": "2000-01-01T00:10:00Z",
            }
            ledger.append_line(json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "schedule-tool-rejected",
                    "name": "mcp__chatds-schedule__schedule_create",
                    "input": arguments,
                }]},
            }).encode(), channel="stdout")
            ledger.append_line(json.dumps({
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "schedule-tool-rejected",
                    "is_error": True,
                    "content": json.dumps({
                        "schema": "chatds.schedule.rejected.v1",
                        "status": "rejected",
                        "code": "schedule_no_occurrence_before_expiry",
                    }),
                }]},
            }).encode(), channel="stdout")
            self.assertEqual(ledger.pending_control_writes, ())
            ledger.close()

    def test_controller_reaps_only_local_bash_not_delegated_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            for task_id, task_type in (
                ("bfixture02", "local_bash"),
                ("afixture02", "local_agent"),
            ):
                ledger.append_line(json.dumps({
                    "type": "system",
                    "subtype": "task_started",
                    "task_id": task_id,
                    "task_type": task_type,
                }).encode(), channel="stdout")
            self.assertEqual(ledger.active_native_task_count, 2)
            self.assertEqual(ledger.reconcile_worker_process_exit(), 1)
            self.assertEqual(ledger.active_native_task_count, 1)
            self.assertEqual(
                ledger.native_task_summary["active_by_type"],
                {"local_agent": 1},
            )
            ledger.close()

    def test_persisted_native_queue_terminal_recovers_renamed_agents(self):
        for task_id, tool_use_id, project_name in (
            ("warehouse-audit-77", "tool-warehouse-77", "-warehouse"),
            ("museum-catalog-91", "tool-museum-91", "-museum"),
        ):
            with self.subTest(task_id=task_id), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "projects"
                native_session_id = str(uuid.uuid4())
                watermark = _native_transcript_watermark(
                    root,
                    native_session_id,
                )
                project = root / project_name
                project.mkdir(parents=True)
                transcript = project / f"{native_session_id}.jsonl"
                content = (
                    "<task-notification>\n"
                    f"<task-id>{task_id}</task-id>\n"
                    f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                    f"<output-file>/runtime/tasks/{task_id}.output</output-file>\n"
                    "<status>completed</status>\n"
                    "<summary>Native worker completed</summary>\n"
                    "<result>domain text is not lifecycle state</result>\n"
                    "</task-notification>"
                )
                transcript.write_text(json.dumps({
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "sessionId": native_session_id,
                    "content": content,
                }) + "\n", encoding="utf-8")

                ledger = EventLedger(Path(temporary) / "events.jsonl")
                ledger.append_line(json.dumps({
                    "type": "system",
                    "subtype": "task_started",
                    "task_id": task_id,
                    "tool_use_id": tool_use_id,
                    "task_type": "local_agent",
                    "session_id": native_session_id,
                }).encode(), channel="stdout")
                ledger.append_line(
                    b'{"type":"result","subtype":"success","is_error":false}',
                    channel="stdout",
                )

                self.assertEqual(ledger.reconcile_native_transcript_queue(
                    projects_root=root,
                    native_session_id=native_session_id,
                    watermark=watermark,
                ), 1)
                self.assertEqual(ledger.active_native_task_count, 0)
                self.assertEqual(
                    ledger.native_task_summary["reconciled_by"][
                        "native_transcript_queue"
                    ],
                    1,
                )
                self.assertEqual(
                    ledger.native_task_summary["terminal_by_status"],
                    {"completed": 1},
                )
                ledger.close()

    def test_native_queue_recovery_rejects_stale_or_unbound_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "projects"
            project = root / "-inventory"
            project.mkdir(parents=True)
            native_session_id = str(uuid.uuid4())
            task_id = "inventory-review-5"
            tool_use_id = "tool-inventory-5"
            transcript = project / f"{native_session_id}.jsonl"
            stale_content = (
                "<task-notification>\n"
                f"<task-id>{task_id}</task-id>\n"
                f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<output-file>/runtime/tasks/{task_id}.output</output-file>\n"
                "<status>completed</status>\n"
                "<summary>Stale terminal</summary>\n"
                "</task-notification>"
            )
            transcript.write_text(json.dumps({
                "type": "queue-operation",
                "operation": "enqueue",
                "sessionId": native_session_id,
                "content": stale_content,
            }) + "\n", encoding="utf-8")
            watermark = _native_transcript_watermark(root, native_session_id)
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "sessionId": native_session_id,
                    "content": stale_content.replace(
                        tool_use_id,
                        "tool-unbound-mutation",
                    ),
                }) + "\n")
                stream.write(json.dumps({
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{
                            "type": "text",
                            "text": "I finished the work.",
                        }],
                    },
                }) + "\n")

            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.append_line(json.dumps({
                "type": "system",
                "subtype": "task_started",
                "task_id": task_id,
                "tool_use_id": tool_use_id,
                "task_type": "local_agent",
                "session_id": native_session_id,
            }).encode(), channel="stdout")
            ledger.append_line(
                b'{"type":"result","subtype":"success","is_error":false}',
                channel="stdout",
            )

            self.assertEqual(ledger.reconcile_native_transcript_queue(
                projects_root=root,
                native_session_id=native_session_id,
                watermark=watermark,
            ), 0)
            self.assertEqual(ledger.active_native_task_count, 1)
            ledger.close()

    def test_native_queue_recovery_requires_successful_native_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "projects"
            native_session_id = str(uuid.uuid4())
            watermark = _native_transcript_watermark(root, native_session_id)
            project = root / "-lab"
            project.mkdir(parents=True)
            task_id = "sample-review-3"
            tool_use_id = "tool-sample-3"
            content = (
                "<task-notification>\n"
                f"<task-id>{task_id}</task-id>\n"
                f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<output-file>/runtime/tasks/{task_id}.output</output-file>\n"
                "<status>completed</status>\n"
                "<summary>Completed</summary>\n"
                "</task-notification>"
            )
            (project / f"{native_session_id}.jsonl").write_text(
                json.dumps({
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "sessionId": native_session_id,
                    "content": content,
                }) + "\n",
                encoding="utf-8",
            )
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.append_line(json.dumps({
                "type": "system",
                "subtype": "task_started",
                "task_id": task_id,
                "tool_use_id": tool_use_id,
                "task_type": "local_agent",
                "session_id": native_session_id,
            }).encode(), channel="stdout")
            ledger.append_line(
                b'{"type":"result","subtype":"error_during_execution",'
                b'"is_error":true}',
                channel="stdout",
            )
            self.assertEqual(ledger.reconcile_native_transcript_queue(
                projects_root=root,
                native_session_id=native_session_id,
                watermark=watermark,
            ), 0)
            self.assertEqual(ledger.active_native_task_count, 1)
            ledger.close()

    def test_artifact_contract_is_machine_checked_only_after_skill_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "RENAMED_DELIVERABLE.md"
            report.write_text("one\ntwo\n", encoding="utf-8")
            after = _workspace_snapshot(root)
            contract = [{
                "skill_name": "inventory-audit",
                "declared_final_artifact": "{PROJECT}_DELIVERABLE.md",
                "expected_min_bytes": 1,
                "expected_min_lines": 4,
                "declared_section_count": 2,
            }]
            inactive = _validate_artifact_contracts(
                contracts=contract,
                invoked_skill_names=frozenset(),
                before={},
                after=after,
                workspace_root=root,
            )
            self.assertEqual(inactive["status"], "not_applicable")
            active = _validate_artifact_contracts(
                contracts=contract,
                invoked_skill_names=frozenset({"inventory-audit"}),
                before={},
                after=after,
                workspace_root=root,
            )
            self.assertEqual(active["status"], "failed")
            self.assertIn(
                "artifact_min_lines_not_met",
                {finding["code"] for finding in active["findings"]},
            )
            self.assertIn(
                "artifact_declared_sections_not_met",
                {finding["code"] for finding in active["findings"]},
            )
            bound = _validate_artifact_contracts(
                contracts=contract,
                invoked_skill_names=frozenset(),
                bound_skill_name="inventory-audit",
                before={},
                after=after,
                workspace_root=root,
            )
            self.assertEqual(bound, active)

    def test_artifact_contract_accepts_renamed_cross_domain_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "renamed-output"
            output.mkdir()
            (output / "01_inventory.md").write_text(
                "module\n", encoding="utf-8"
            )
            report = output / "WAREHOUSE_FULL.md"
            report.write_text("# One\nbody\n## Two\nbody\n", encoding="utf-8")
            after = _workspace_snapshot(root)
            receipt = _validate_artifact_contracts(
                contracts=[{
                    "skill_name": "warehouse-planner",
                    "declared_final_artifact": "{NAME}_FULL.md",
                    "declared_modular_files": ["01_*.md"],
                    "expected_min_bytes": 4,
                    "expected_min_lines": 4,
                    "declared_section_count": 2,
                }],
                invoked_skill_names=frozenset(),
                bound_skill_name="warehouse-planner",
                before={},
                after=after,
                workspace_root=root,
            )
            self.assertEqual(receipt["status"], "passed")

            (output / "01_inventory.md").unlink()
            archive = root / "unrelated-archive"
            archive.mkdir()
            (archive / "01_inventory.md").write_text(
                "stale module\n", encoding="utf-8"
            )
            missing = _validate_artifact_contracts(
                contracts=[{
                    "skill_name": "warehouse-planner",
                    "declared_final_artifact": "{NAME}_FULL.md",
                    "declared_modular_files": ["01_*.md"],
                }],
                invoked_skill_names=frozenset(),
                bound_skill_name="warehouse-planner",
                before={},
                after=_workspace_snapshot(root),
                workspace_root=root,
            )
            self.assertEqual(missing["status"], "failed")
            self.assertEqual(
                missing["findings"],
                [{
                    "code": "artifact_declared_module_missing",
                    "skill_name": "warehouse-planner",
                    "pattern": "01_*.md",
                    "final_parent": "renamed-output",
                }],
            )

    def test_bound_contract_uses_one_native_stop_hook_without_control_prompt(self):
        config = _config()
        config["permission_preset"] = "session_full"
        config["artifact_contracts"] = [{
            "skill_name": "warehouse-planner",
            "declared_final_artifact": "{NAME}_FULL.md",
            "declared_modular_files": ["01_*.md"],
            "expected_min_lines": 4,
        }, {
            "skill_name": "unrelated-package",
            "declared_final_artifact": "OTHER.md",
        }]
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = Path(temporary)
            contract_path = _materialize_bound_artifact_stop_contract(
                config=config,
                turn_skill_binding=(
                    "warehouse-planner",
                    "fresh_session_primary",
                ),
                workspace_before={},
                runtime_root=runtime_root,
                controller_uid=os.getuid(),
                worker_gid=os.getgid(),
            )
            self.assertIsNotNone(contract_path)
            assert contract_path is not None
            self.assertEqual(
                stat.S_IMODE(os.lstat(contract_path).st_mode),
                0o440,
            )
            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["skill_name"], "warehouse-planner")
            self.assertEqual(len(payload["contracts"]), 1)

        command, _ = _claude_command(
            config,
            artifact_stop_contract_path=Path(
                "/runtime/artifact-stop-contract.json"
            ),
        )
        self.assertNotIn("--append-system-prompt", command)
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(set(settings), {"hooks"})
        stop_hooks = settings["hooks"]["Stop"]
        self.assertEqual(len(stop_hooks), 1)
        command_hook = stop_hooks[0]["hooks"][0]
        self.assertEqual(command_hook["type"], "command")
        self.assertIn("claude_runner.artifact_stop_hook", command_hook["command"])

    def test_bound_workflow_uses_native_pretooluse_and_stop_hooks(self):
        config = _config()
        config["permission_preset"] = "session_full"
        command, _ = _claude_command(
            config,
            native_lifecycle_contract_path=Path(
                "/runtime/native-lifecycle-contract.json"
            ),
        )
        self.assertNotIn("--append-system-prompt", command)
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(set(settings["hooks"]), {"PreToolUse", "Stop"})
        pre_tool = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(
            pre_tool["matcher"],
            "Agent|Task|Write|Edit|MultiEdit|NotebookEdit|Bash",
        )
        commands = [
            settings["hooks"][event][0]["hooks"][0]["command"]
            for event in ("PreToolUse", "Stop")
        ]
        self.assertEqual(commands[0], commands[1])
        self.assertIn("claude_runner.native_lifecycle_hook", commands[0])

    def test_native_lifecycle_contract_preserves_phase_before_artifact_order(self):
        workflow = {
            "schema": "chatds.skill-workflow-contract.v1",
            "skill_name": "museum-audit",
            "route_id": "collection_review",
            "source_path": "orchestration/orchestrator.yml",
            "priority": 3,
            "matched_pattern_index": 0,
            "route_sha256": "c" * 64,
            "phases": [{
                "mode": "sequential",
                "workers": [{
                    "worker_id": "curator",
                    "native_agent_type": (
                        "chatds-session-skills:museum-audit:curator"
                    ),
                }],
            }],
        }
        artifact = {
            "skill_name": "museum-audit",
            "declared_final_artifact": "{NAME}_FINAL.md",
            "expected_min_lines": 3,
        }
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            runtime.mkdir(mode=0o700)
            config = _config()
            config["workflow_contract"] = workflow
            config["artifact_contracts"] = [artifact]
            path = _materialize_bound_native_lifecycle_contract(
                config=config,
                turn_skill_binding=("museum-audit", "fresh_session_primary"),
                workspace_before={},
                runtime_root=runtime,
                controller_uid=os.getuid(),
                worker_gid=os.getgid(),
            )
            self.assertIsNotNone(path)
            assert path is not None
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["schema"], "chatds.native-lifecycle-contract.v1"
            )
            self.assertEqual(
                payload["workflow_synthesis_baseline_path"],
                "/runtime/workflow-synthesis-baseline.json",
            )
            receipt = build_workflow_receipt(workflow, [])
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            stop = evaluate_lifecycle_hook(
                hook_input={
                    "hook_event_name": "Stop",
                    "stop_hook_active": False,
                },
                contract=payload,
                workflow_receipt=receipt,
                workspace_root=workspace,
            )
            self.assertIn("workflow_worker_missing", stop["reason"])
            self.assertNotIn("artifact_final_missing", stop["reason"])
            self.assertEqual(
                evaluate_lifecycle_hook(
                    hook_input={
                        "hook_event_name": "Stop",
                        "stop_hook_active": True,
                    },
                    contract=payload,
                    workflow_receipt=receipt,
                    workspace_root=workspace,
                ),
                {},
            )

            passed = build_workflow_receipt(workflow, [{
                "native_agent_type": (
                    "chatds-session-skills:museum-audit:curator"
                ),
                "status": "completed",
            }])
            synthesis_baseline = {
                "schema": "chatds.workflow-synthesis-baseline.v1",
                "skill_name": workflow["skill_name"],
                "route_id": workflow["route_id"],
                "route_sha256": workflow["route_sha256"],
                "workspace_before": {},
                "final_content_sha256": {},
            }
            invalid_baseline = dict(synthesis_baseline)
            invalid_baseline["route_sha256"] = "0" * 64
            invalid_pretool = evaluate_lifecycle_hook(
                hook_input={
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "true"},
                },
                contract=payload,
                workflow_receipt=passed,
                workflow_synthesis_baseline=invalid_baseline,
                workspace_root=workspace,
            )
            self.assertEqual(
                invalid_pretool["hookSpecificOutput"]
                ["permissionDecision"],
                "deny",
            )
            artifact_stop = evaluate_lifecycle_hook(
                hook_input={
                    "hook_event_name": "Stop",
                    "stop_hook_active": False,
                },
                contract=payload,
                workflow_receipt=passed,
                workflow_synthesis_baseline=synthesis_baseline,
                workspace_root=workspace,
            )
            self.assertIn("artifact_final_missing", artifact_stop["reason"])

    def test_workflow_pass_snapshot_rejects_pre_frontier_artifact_bypass(self):
        """Arbitrary shell languages cannot define the synthesis epoch.

        This renamed warehouse fixture reproduces the production class of
        failure: a Bash command can hide a declared path inside interpreter
        syntax that the best-effort PreToolUse recognizer cannot parse.  The
        machine-owned workflow transition, rather than command spelling, must
        therefore be the authoritative artifact baseline.
        """

        workflow = {
            "schema": "chatds.skill-workflow-contract.v1",
            "skill_name": "warehouse-reconciliation-renamed",
            "route_id": "inventory_closeout",
            "source_path": "orchestration/orchestrator.yml",
            "priority": 2,
            "matched_pattern_index": 0,
            "route_sha256": "d" * 64,
            "phases": [{
                "mode": "sequential",
                "workers": [{
                    "worker_id": "stock-reviewer",
                    "native_agent_type": (
                        "chatds-session-skills:"
                        "warehouse-reconciliation-renamed:stock-reviewer"
                    ),
                }],
            }],
        }
        artifact = {
            "skill_name": "warehouse-reconciliation-renamed",
            "declared_final_artifact": "deliveries/{NAME}_CLOSEOUT.md",
            "expected_min_lines": 2,
        }
        pending = build_workflow_receipt(workflow, [])
        hidden_path_command = (
            "python -c \"from pathlib import Path; "
            "Path('/workspace/deliveries/WAREHOUSE_CLOSEOUT.md')"
            ".write_text('early\\nartifact\\n')\""
        )
        self.assertEqual(
            evaluate_workflow_hook(
                hook_input={
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_use_id": "interpreter-bypass",
                    "tool_input": {"command": hidden_path_command},
                },
                contract=workflow,
                receipt=pending,
                artifact_contracts=[artifact],
            ),
            {},
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            deliveries = workspace / "deliveries"
            deliveries.mkdir(parents=True)
            final = deliveries / "WAREHOUSE_CLOSEOUT.md"
            final.write_text("early\nartifact\n", encoding="utf-8")

            native_session_id = str(uuid.uuid4())
            projects = root / "projects"
            project = projects / "fixture"
            subagents = project / native_session_id / "subagents"
            subagents.mkdir(parents=True)
            (project / f"{native_session_id}.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )

            receipt_path = root / "workflow-receipt.json"
            baseline_path = root / "workflow-synthesis-baseline.json"
            ledger = EventLedger(root / "events.jsonl")
            ledger.bind_native_task_state(
                projects_root=projects,
                native_session_id=native_session_id,
            )
            ledger.bind_workflow_contract(
                contract=workflow,
                receipt_path=receipt_path,
                synthesis_baseline_path=baseline_path,
                workspace_root=workspace,
                synthesis_artifact_contracts=[artifact],
            )

            agent_type = workflow["phases"][0]["workers"][0][
                "native_agent_type"
            ]
            task_id = "agent-stock-review"
            tool_id = "tool-stock-review"
            ledger.append_line(json.dumps({
                "type": "system",
                "subtype": "task_started",
                "task_id": task_id,
                "task_type": "local_agent",
                "tool_use_id": tool_id,
                "session_id": native_session_id,
                "subagent_type": agent_type,
            }).encode(), channel="stdout")
            (subagents / f"agent-{task_id}.meta.json").write_text(
                json.dumps({
                    "agentType": agent_type,
                    "description": "renamed holdout fixture",
                    "toolUseId": tool_id,
                }),
                encoding="utf-8",
            )
            (subagents / f"agent-{task_id}.jsonl").write_text(
                json.dumps({
                    "type": "assistant",
                    "agentId": task_id,
                    "sessionId": native_session_id,
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{
                            "type": "text",
                            "text": "Inventory evidence complete.",
                        }],
                    },
                }) + "\n",
                encoding="utf-8",
            )
            ledger.append_line(json.dumps({
                "type": "system",
                "subtype": "task_updated",
                "task_id": task_id,
                "patch": {"status": "completed"},
            }).encode(), channel="stdout")

            self.assertEqual(
                json.loads(receipt_path.read_text())["status"], "passed"
            )
            baseline = ledger.workflow_synthesis_baseline
            content_baseline = ledger.workflow_synthesis_content_sha256
            self.assertIsNotNone(baseline)
            self.assertIsNotNone(content_baseline)
            assert baseline is not None
            assert content_baseline is not None
            self.assertIn(
                "deliveries/WAREHOUSE_CLOSEOUT.md", baseline
            )
            stored = json.loads(baseline_path.read_text(encoding="utf-8"))
            self.assertEqual(
                stat.S_IMODE(os.lstat(baseline_path).st_mode), 0o440
            )
            self.assertEqual(
                stored["schema"],
                "chatds.workflow-synthesis-baseline.v1",
            )
            self.assertEqual(
                stored["final_content_sha256"], content_baseline
            )
            passed_receipt = json.loads(
                receipt_path.read_text(encoding="utf-8")
            )
            lifecycle_contract = {
                "schema": "chatds.native-lifecycle-contract.v1",
                "skill_name": workflow["skill_name"],
                "artifact_contracts": [artifact],
                "workspace_before": {},
                "workflow_contract": workflow,
                "workflow_receipt_path": "/runtime/workflow-receipt.json",
                "workflow_synthesis_baseline_path": (
                    "/runtime/workflow-synthesis-baseline.json"
                ),
            }
            early_stop = evaluate_lifecycle_hook(
                hook_input={
                    "hook_event_name": "Stop",
                    "stop_hook_active": False,
                },
                contract=lifecycle_contract,
                workflow_receipt=passed_receipt,
                workflow_synthesis_baseline=stored,
                workspace_root=workspace,
            )
            self.assertIn(
                "artifact_final_not_committed_after_workflow",
                early_stop["reason"],
            )

            early = _validate_artifact_contracts(
                contracts=[artifact],
                invoked_skill_names=frozenset(),
                bound_skill_name=workflow["skill_name"],
                before=baseline,
                before_content_sha256=content_baseline,
                after=_workspace_snapshot(workspace),
                workspace_root=workspace,
            )
            self.assertEqual(early["status"], "failed")
            self.assertEqual(
                early["findings"][0]["code"],
                "artifact_final_not_committed_after_workflow",
            )

            original_mtime = baseline[
                "deliveries/WAREHOUSE_CLOSEOUT.md"
            ][4]
            os.utime(
                final,
                ns=(original_mtime + 1_000_000_000,) * 2,
            )
            touched_after = _workspace_snapshot(workspace)
            self.assertNotEqual(
                touched_after["deliveries/WAREHOUSE_CLOSEOUT.md"],
                baseline["deliveries/WAREHOUSE_CLOSEOUT.md"],
            )
            touched = _validate_artifact_contracts(
                contracts=[artifact],
                invoked_skill_names=frozenset(),
                bound_skill_name=workflow["skill_name"],
                before=baseline,
                before_content_sha256=content_baseline,
                after=touched_after,
                workspace_root=workspace,
            )
            self.assertEqual(touched["status"], "failed")

            final.write_text(
                "post-frontier\ncloseout artifact\n", encoding="utf-8"
            )
            committed = _validate_artifact_contracts(
                contracts=[artifact],
                invoked_skill_names=frozenset(),
                bound_skill_name=workflow["skill_name"],
                before=baseline,
                before_content_sha256=content_baseline,
                after=_workspace_snapshot(workspace),
                workspace_root=workspace,
            )
            self.assertEqual(committed["status"], "passed")
            self.assertEqual(
                evaluate_lifecycle_hook(
                    hook_input={
                        "hook_event_name": "Stop",
                        "stop_hook_active": False,
                    },
                    contract=lifecycle_contract,
                    workflow_receipt=passed_receipt,
                    workflow_synthesis_baseline=stored,
                    workspace_root=workspace,
                ),
                {},
            )
            ledger.close()

    def test_native_stop_feedback_is_bounded_and_cross_domain(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            output = workspace / "renamed-output"
            output.mkdir()
            (output / "01_inventory.md").write_text(
                "module\n", encoding="utf-8"
            )
            report = output / "WAREHOUSE_FULL.md"
            report.write_text("# One\nbody\n", encoding="utf-8")
            contract = {
                "schema": "chatds.artifact-stop-contract.v1",
                "skill_name": "warehouse-planner",
                "workspace_before": {},
                "contracts": [{
                    "skill_name": "warehouse-planner",
                    "declared_final_artifact": "{NAME}_FULL.md",
                    "declared_modular_files": ["01_*.md"],
                    "expected_min_lines": 4,
                    "declared_section_count": 2,
                }],
            }
            first = evaluate_stop_hook(
                hook_input={
                    "hook_event_name": "Stop",
                    "stop_hook_active": False,
                },
                contract=contract,
                workspace_root=workspace,
            )
            self.assertEqual(first["decision"], "block")
            self.assertIn("artifact_min_lines_not_met", first["reason"])
            self.assertIn("artifact_declared_sections_not_met", first["reason"])

            stale_contract = dict(contract)
            stale_contract["workspace_before"] = {
                relative: list(identity)
                for relative, identity in _workspace_snapshot(workspace).items()
            }
            stale = evaluate_stop_hook(
                hook_input={
                    "hook_event_name": "Stop",
                    "stop_hook_active": False,
                },
                contract=stale_contract,
                workspace_root=workspace,
            )
            self.assertEqual(stale["decision"], "block")
            self.assertIn(
                "artifact_final_not_committed_this_turn", stale["reason"]
            )

            # Claude Code marks the native continuation.  The hook never
            # creates an adapter-owned retry loop; the terminal audit remains
            # the fail-closed authority after this single correction pass.
            self.assertEqual(
                evaluate_stop_hook(
                    hook_input={
                        "hook_event_name": "Stop",
                        "stop_hook_active": True,
                    },
                    contract=contract,
                    workspace_root=workspace,
                ),
                {},
            )

            report.write_text(
                "# One\nbody\n## Two\nbody\n", encoding="utf-8"
            )
            self.assertEqual(
                evaluate_stop_hook(
                    hook_input={
                        "hook_event_name": "Stop",
                        "stop_hook_active": False,
                    },
                    contract=contract,
                    workspace_root=workspace,
                ),
                {},
            )

    def test_current_final_is_selected_without_deleting_prior_deliverables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prior = root / "PRIOR_FULL.md"
            prior.write_text("# Prior\n", encoding="utf-8")
            before = _workspace_snapshot(root)
            current = root / "CURRENT_FULL.md"
            current.write_text("# Current\n", encoding="utf-8")
            after = _workspace_snapshot(root)
            receipt = _validate_artifact_contracts(
                contracts=[{
                    "skill_name": "portable-planner",
                    "declared_final_artifact": "{NAME}_FULL.md",
                    "declared_section_count": 1,
                }],
                invoked_skill_names=frozenset({"portable-planner"}),
                before=before,
                after=after,
                workspace_root=root,
            )
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["validated"][0]["path"], "CURRENT_FULL.md")

    def test_workspace_snapshot_emits_only_new_or_mutated_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            unchanged = workspace / "unchanged.txt"
            unchanged.write_text("same", encoding="utf-8")
            changed = workspace / "changed.md"
            changed.write_text("old", encoding="utf-8")
            before = _workspace_snapshot(workspace)

            changed.write_text("new content", encoding="utf-8")
            nested = workspace / "reports" / "final.md"
            nested.parent.mkdir()
            nested.write_text("result", encoding="utf-8")
            after = _workspace_snapshot(workspace)

            ledger_path = root / "events.jsonl"
            ledger = EventLedger(ledger_path)
            _emit_workspace_artifacts(
                ledger=ledger,
                run_id="a" * 32,
                before=before,
                after=after,
                workspace_root=workspace,
            )
            ledger.close()
            events = [
                json.loads(line)["event"]
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["path"] for event in events],
                ["changed.md", "reports/final.md"],
            )
            self.assertTrue(all(
                event["type"] == "chatds.workspace.artifact"
                and len(event["sha256"]) == 64
                and event["source_event_key"].startswith(
                    "claude-workspace:" + "a" * 32
                )
                for event in events
            ))

    def test_workspace_snapshot_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "target").write_text("target", encoding="utf-8")
            (workspace / "link").symlink_to("target")
            with self.assertRaisesRegex(
                RuntimeError,
                "workspace_artifact_symlink_invalid",
            ):
                _workspace_snapshot(workspace)

    def test_candidate_checkpoint_must_be_one_regular_nonempty_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_id = str(uuid.uuid4())
            project = root / "project"
            project.mkdir()
            transcript = project / f"{session_id}.jsonl"
            self.assertFalse(_native_checkpoint_exists(root, session_id))
            transcript.write_text("{}\n", encoding="utf-8")
            self.assertTrue(_native_checkpoint_exists(root, session_id))
            duplicate = root / "other"
            duplicate.mkdir()
            (duplicate / transcript.name).write_text("{}\n", encoding="utf-8")
            self.assertFalse(_native_checkpoint_exists(root, session_id))


class RunnerEgressPolicyTests(unittest.TestCase):
    def _view(
        self,
        root: Path,
        *,
        mcp_servers: dict | None = None,
        platform_egress_rules: list[dict] | None = None,
    ):
        view = root / "view"
        skill = view / "plugin" / "skills" / "fixture"
        descriptor = view / "plugin" / ".claude-plugin" / "plugin.json"
        skill.mkdir(parents=True)
        descriptor.parent.mkdir(parents=True)
        skill_file = skill / "SKILL.md"
        skill_file.write_text(
            "Use GET https://clinicaltrials.gov/api/v2/studies/ and "
            "POST https://api.example.org/graphql for this workflow.",
            encoding="utf-8",
        )
        descriptor.write_text(
            json.dumps({"name": "fixture", "version": "1.0.0"}),
            encoding="utf-8",
        )
        rows = []
        for path in (skill_file, descriptor):
            payload = path.read_bytes()
            rows.append({
                "path": path.relative_to(view).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            })
        if mcp_servers is not None:
            mcp_path = view / "plugin" / ".mcp.json"
            mcp_path.write_text(
                json.dumps({"mcpServers": mcp_servers}),
                encoding="utf-8",
            )
            payload = mcp_path.read_bytes()
            rows.append({
                "path": mcp_path.relative_to(view).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            })
        identity = {
            "schema": "chatds.claude-skill-view.v1",
            **(
                {"platform_egress_rules": platform_egress_rules}
                if platform_egress_rules is not None
                else {}
            ),
            "skills": [{
                "name": "fixture",
                "scope": "session",
                "bundle_id": None,
                "bundle_role": None,
                "files": [{
                    "path": "SKILL.md",
                    "sha256": rows[0]["sha256"],
                    "size": rows[0]["size"],
                }],
            }],
            "files": sorted(rows, key=lambda row: row["path"]),
        }
        digest = hashlib.sha256(json.dumps(
            identity,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        (view / "manifest.json").write_text(
            json.dumps({**identity, "sha256": digest}),
            encoding="utf-8",
        )
        for walk_root, directories, files in os.walk(view, topdown=False):
            for name in files:
                os.chmod(Path(walk_root) / name, 0o444)
            for name in directories:
                os.chmod(Path(walk_root) / name, 0o555)
        os.chmod(view, 0o555)
        return view, digest

    def test_platform_web_capability_has_exact_rule_and_private_origin_gate(self):
        rule = {
            "capability": "web_search",
            "url_prefix": "http://search.internal:8080/search",
            "methods": ["GET"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(
                Path(temporary),
                platform_egress_rules=[rule],
            )
            common = dict(
                skill_view_root=view,
                skill_view_sha256=digest,
                user_turn_text="generic current-information request",
                provider_base_url="https://api.example.test",
                budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                limits={
                    "max_outbound_bytes": 1024,
                    "max_requests": 10,
                    "max_response_wire_bytes": 4096,
                },
            )
            denied = compile_turn_egress_policy(
                configured_private_origins=(),
                **common,
            )
            allowed = compile_turn_egress_policy(
                configured_private_origins=("http://search.internal:8080",),
                **common,
            )
        search_rule = next(
            row for row in allowed["egress_rules"]
            if row["url_prefix"] == "http://search.internal:8080/search"
        )
        self.assertEqual(search_rule["methods"], ["GET"])
        self.assertNotIn(
            "http://search.internal:8080",
            denied["private_origins"],
        )
        self.assertIn(
            "http://search.internal:8080",
            allowed["private_origins"],
        )

    def test_openai_native_engine_receives_only_exact_completion_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(Path(temporary))
            common = dict(
                skill_view_root=view,
                skill_view_sha256=digest,
                user_turn_text="produce a cross-domain artifact",
                provider_base_url="https://provider.example.test/v1",
                configured_private_origins=(),
                budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                limits={
                    "max_outbound_bytes": 1024,
                    "max_requests": 10,
                    "max_response_wire_bytes": 4096,
                },
            )
            policy = compile_turn_egress_policy(
                provider_protocol="openai",
                provider_response_idle_timeout_seconds=7_260,
                **common,
            )
            with self.assertRaises(ClaudeEgressPolicyError):
                compile_turn_egress_policy(
                    provider_protocol="renamed-unsupported-protocol",
                    **common,
                )
        provider_rules = [
            row for row in policy["egress_rules"]
            if urlsplit(row["url_prefix"]).hostname == "provider.example.test"
        ]
        self.assertEqual(len(provider_rules), 1)
        self.assertEqual(
            urlsplit(provider_rules[0]["url_prefix"]).path,
            "/v1/chat/completions",
        )
        self.assertEqual(provider_rules[0]["methods"], ["POST"])
        self.assertIs(provider_rules[0]["query_exact"], True)
        self.assertEqual(
            provider_rules[0]["response_idle_timeout_seconds"],
            7_260,
        )
        self.assertFalse(any(
            "response_idle_timeout_seconds" in row
            for row in policy["egress_rules"]
            if row not in provider_rules
        ))

    def test_public_read_profile_is_signed_data_not_wildcard_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(Path(temporary))
            policy = compile_turn_egress_policy(
                skill_view_root=view,
                skill_view_sha256=digest,
                user_turn_text="inspect a renamed public documentation site",
                provider_base_url="https://api.example.test",
                configured_private_origins=(),
                budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                limits={
                    "max_outbound_bytes": 1024,
                    "max_requests": 10,
                    "max_response_wire_bytes": 4096,
                },
                public_read_enabled=True,
            )
        self.assertEqual(policy["public_read"], {
            "methods": ["GET", "HEAD"],
            "ports": [80, 443],
        })
        self.assertFalse(any(
            "*" in row["url_prefix"]
            for row in policy["egress_rules"]
        ))

    def test_typed_market_capability_has_only_internal_gateway_authority(self):
        rule = {
            "capability": "market_quote",
            "url_prefix": "http://market-data.internal:8090/v1/quote",
            "methods": ["GET"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(
                Path(temporary),
                platform_egress_rules=[rule],
            )
            policy = compile_turn_egress_policy(
                skill_view_root=view,
                skill_view_sha256=digest,
                user_turn_text="latest quote without any URL",
                provider_base_url="https://api.example.test",
                configured_private_origins=(
                    "http://market-data.internal:8090",
                ),
                budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                limits={
                    "max_outbound_bytes": 1024,
                    "max_requests": 10,
                    "max_response_wire_bytes": 4096,
                },
            )
        self.assertIn(
            "http://market-data.internal:8090",
            policy["private_origins"],
        )
        market_rules = [
            row for row in policy["egress_rules"]
            if "market-data.internal" in row["url_prefix"]
        ]
        self.assertEqual(market_rules, [{
            "url_prefix": "http://market-data.internal:8090/v1/quote",
            "methods": ["GET"],
        }])
        self.assertFalse(any(
            provider in row["url_prefix"]
            for row in policy["egress_rules"]
            for provider in ("sinajs", "gtimg", "eastmoney")
        ))

    def test_unknown_platform_capability_cannot_mint_egress(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(
                Path(temporary),
                platform_egress_rules=[{
                    "capability": "arbitrary_http",
                    "url_prefix": "https://attacker.invalid/",
                    "methods": ["GET"],
                }],
            )
            with self.assertRaisesRegex(
                ClaudeEgressPolicyError,
                "Platform I/O capability rule is malformed",
            ):
                compile_turn_egress_policy(
                    skill_view_root=view,
                    skill_view_sha256=digest,
                    user_turn_text="fixture",
                    provider_base_url="https://api.example.test",
                    configured_private_origins=(),
                    budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                    call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                    limits={
                        "max_outbound_bytes": 1024,
                        "max_requests": 10,
                        "max_response_wire_bytes": 4096,
                    },
                )

    def test_policy_has_only_declared_user_and_provider_coordinates(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(Path(temporary))
            policy = compile_turn_egress_policy(
                skill_view_root=view,
                skill_view_sha256=digest,
                user_turn_text="Read https://news.example.net/item/42",
                provider_base_url="https://api.shaiengine.com",
                configured_private_origins=(),
                budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                limits={
                    "max_outbound_bytes": 1024,
                    "max_requests": 10,
                    "max_response_wire_bytes": 4096,
                },
            )
        rules = {
            (row["url_prefix"], tuple(row["methods"]))
            for row in policy["egress_rules"]
        }
        self.assertIn(
            ("https://api.shaiengine.com:443/v1/messages", ("POST",)),
            rules,
        )
        self.assertIn(
            (
                "https://api.shaiengine.com:443/v1/messages?beta=true",
                ("POST",),
            ),
            rules,
        )
        self.assertIn(
            (
                "https://api.shaiengine.com:443/v1/messages/count_tokens",
                ("POST",),
            ),
            rules,
        )
        self.assertIn(
            ("https://news.example.net:443/item/42", ("GET", "HEAD")),
            rules,
        )
        for row in policy["egress_rules"]:
            if (
                row["url_prefix"].startswith(
                    "https://api.shaiengine.com:443/v1/messages"
                )
                or row["url_prefix"]
                == "https://news.example.net:443/item/42"
            ):
                self.assertIs(row.get("query_exact"), True)
        self.assertFalse(any("telemetry" in prefix for prefix, _methods in rules))
        self.assertEqual(policy["policy_version"], 3)

    def test_private_origin_needs_deployment_and_current_user_intersection(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(Path(temporary))
            common = dict(
                skill_view_root=view,
                skill_view_sha256=digest,
                provider_base_url="https://api.shaiengine.com",
                configured_private_origins=("https://10.10.132.126:18443",),
                budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                limits={
                    "max_outbound_bytes": 1024,
                    "max_requests": 10,
                    "max_response_wire_bytes": 4096,
                },
            )
            absent = compile_turn_egress_policy(user_turn_text="no private URL", **common)
            present = compile_turn_egress_policy(
                user_turn_text="Read https://10.10.132.126:18443/api/data",
                **common,
            )
        self.assertEqual(absent["private_origins"], [])
        self.assertEqual(present["private_origins"], ["https://10.10.132.126:18443"])

    def test_private_provider_gets_only_exact_messages_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(Path(temporary))
            policy = compile_turn_egress_policy(
                skill_view_root=view,
                skill_view_sha256=digest,
                user_turn_text="fixture",
                provider_base_url="http://10.10.132.2:1025",
                configured_private_origins=("http://10.10.132.2:1025",),
                budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                limits={
                    "max_outbound_bytes": 1024,
                    "max_requests": 10,
                    "max_response_wire_bytes": 4096,
                },
            )
        provider_rules = [
            row for row in policy["egress_rules"]
            if "10.10.132.2" in row["url_prefix"]
        ]
        self.assertEqual(provider_rules, [
            {
                "url_prefix": "http://10.10.132.2:1025/v1/messages",
                "methods": ["POST"],
                "query_exact": True,
            },
            {
                "url_prefix": (
                    "http://10.10.132.2:1025/v1/messages?beta=true"
                ),
                "methods": ["POST"],
                "query_exact": True,
            },
            {
                "url_prefix": (
                    "http://10.10.132.2:1025/v1/messages/count_tokens"
                ),
                "methods": ["POST"],
                "query_exact": True,
            },
            {
                "url_prefix": (
                    "http://10.10.132.2:1025/v1/messages/"
                    "count_tokens?beta=true"
                ),
                "methods": ["POST"],
                "query_exact": True,
            },
        ])
        self.assertEqual(
            policy["private_origins"], ["http://10.10.132.2:1025"]
        )

    def test_user_url_never_grants_write_or_neighbor_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(Path(temporary))
            policy = compile_turn_egress_policy(
                skill_view_root=view,
                skill_view_sha256=digest,
                user_turn_text="Read https://news.example.net/item/42?view=full",
                provider_base_url="https://api.shaiengine.com",
                configured_private_origins=(),
                budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                limits={
                    "max_outbound_bytes": 1024,
                    "max_requests": 10,
                    "max_response_wire_bytes": 4096,
                },
            )
        user_rule = next(
            row for row in policy["egress_rules"]
            if "news.example.net" in row["url_prefix"]
        )
        self.assertEqual(user_rule["methods"], ["GET", "HEAD"])
        self.assertIs(user_rule["query_exact"], True)
        self.assertIn("/item/42?view=full", user_rule["url_prefix"])
        self.assertNotEqual(user_rule["url_prefix"], "https://news.example.net:443/")

    def test_tampered_immutable_skill_view_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(Path(temporary))
            skill_file = view / "plugin" / "skills" / "fixture" / "SKILL.md"
            os.chmod(skill_file, 0o644)
            skill_file.write_text("https://attacker.invalid/", encoding="utf-8")
            os.chmod(skill_file, 0o444)
            with self.assertRaises(ClaudeEgressPolicyError):
                compile_turn_egress_policy(
                    skill_view_root=view,
                    skill_view_sha256=digest,
                    user_turn_text="fixture",
                    provider_base_url="https://api.shaiengine.com",
                    configured_private_origins=(),
                    budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                    call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                    limits={
                        "max_outbound_bytes": 1024,
                        "max_requests": 10,
                        "max_response_wire_bytes": 4096,
                    },
                )

    def test_explicit_remote_mcp_gets_only_protocol_endpoint_methods(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(
                Path(temporary),
                mcp_servers={
                    "evidence": {
                        "type": "http",
                        "url": "https://mcp.example.test/v1/mcp",
                    },
                },
            )
            policy = compile_turn_egress_policy(
                skill_view_root=view,
                skill_view_sha256=digest,
                user_turn_text="fixture",
                provider_base_url="https://api.shaiengine.com",
                configured_private_origins=(),
                budget_scope_sha256=hashlib.sha256(b"budget").hexdigest(),
                call_id_sha256=hashlib.sha256(b"call").hexdigest(),
                limits={
                    "max_outbound_bytes": 1024,
                    "max_requests": 10,
                    "max_response_wire_bytes": 4096,
                },
            )
        rule = next(
            row for row in policy["egress_rules"]
            if "mcp.example.test" in row["url_prefix"]
        )
        self.assertEqual(
            rule,
            {
                "url_prefix": "https://mcp.example.test:443/v1/mcp",
                "methods": ["GET", "HEAD", "OPTIONS", "POST", "DELETE"],
            },
        )


if __name__ == "__main__":
    unittest.main()
