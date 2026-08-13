import base64
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from chatds_browser_runtime.proxy_bridge import BridgeConfigurationError

from claude_runner.policy import (
    ClaudeEgressPolicyError,
    compile_turn_egress_policy,
)
from claude_runner.runner_entrypoint import (
    EventLedger,
    _claude_command,
    _native_checkpoint_exists,
    _pending_plan_task_count,
    _quarantine_native_cron_state,
    _safe_controller_exception_code,
    _terminal_error,
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
        capability_prompt = command[
            command.index("--append-system-prompt") + 1
        ]
        self.assertIn("renamed_lookup", capability_prompt)
        self.assertIn("supersedes", capability_prompt)
        self.assertNotEqual(capability_prompt, fresh["prompt"])
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
        self.assertIn("--append-system-prompt", command)

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
                "Bash": None,
                "mcp__chatds-market-data__market_quote": "market_quote",
            }
            arguments = {
                "name": "Renamed public metric monitor",
                "prompt": "Report the selected public metric.",
                "schedule": "every 5m",
                "timezone": "UTC",
                "enabled_tools": [
                    "Bash",
                    "mcp__chatds-market-data__market_quote",
                ],
            }
            ledger = EventLedger(Path(temporary) / "events.jsonl")
            ledger.bind_schedule_tool_aliases(aliases)
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
                SCHEDULE_TOOL_ALIASES_ENV,
                _accepted_receipt,
            )
            with patch.dict(os.environ, {
                SCHEDULE_TOOL_ALIASES_ENV: json.dumps(aliases),
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
                ledger.pending_control_writes[0]["request"]["enabled_tools"],
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

    def test_artifact_contract_accepts_renamed_cross_domain_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "01_inventory.md").write_text("module\n", encoding="utf-8")
            report = root / "WAREHOUSE_FULL.md"
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
                invoked_skill_names=frozenset({"warehouse-planner"}),
                before={},
                after=after,
                workspace_root=root,
            )
            self.assertEqual(receipt["status"], "passed")

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
        harness_egress_rules: list[dict] | None = None,
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
                {"harness_egress_rules": harness_egress_rules}
                if harness_egress_rules is not None
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

    def test_harness_web_capability_has_exact_rule_and_private_origin_gate(self):
        rule = {
            "capability": "web_search",
            "url_prefix": "http://search.internal:8080/search",
            "methods": ["GET"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(
                Path(temporary),
                harness_egress_rules=[rule],
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
                harness_egress_rules=[rule],
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

    def test_unknown_harness_capability_cannot_mint_egress(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, digest = self._view(
                Path(temporary),
                harness_egress_rules=[{
                    "capability": "arbitrary_http",
                    "url_prefix": "https://attacker.invalid/",
                    "methods": ["GET"],
                }],
            )
            with self.assertRaisesRegex(
                ClaudeEgressPolicyError,
                "Harness capability rule is malformed",
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
