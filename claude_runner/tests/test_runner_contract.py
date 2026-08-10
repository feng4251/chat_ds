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
    _safe_controller_exception_code,
    _terminal_error,
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

    def test_fresh_and_resumed_turns_use_transactional_native_sessions(self):
        fresh = _config()
        command, prompt = _claude_command(fresh)
        self.assertEqual(prompt, b"test")
        self.assertNotIn("--bare", command)
        self.assertIn("--no-chrome", command)
        self.assertIn("--thinking", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(
            command[command.index("--mcp-config") + 1],
            "/skill-view/plugin/.mcp.json",
        )
        self.assertEqual(command[command.index("--tools") + 1], "default")
        self.assertEqual(
            command[command.index("--disallowedTools") + 1],
            "WebFetch,WebSearch",
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

        native_web = {**fresh, "native_web_tools": True}
        command, _ = _claude_command(native_web)
        self.assertNotIn("--disallowedTools", command)

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

    def test_native_subtask_lifecycle_and_plan_tasks_gate_success(self):
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
            (task_root / "1.json").write_text(
                json.dumps({"status": "completed"}),
                encoding="utf-8",
            )
            self.assertEqual(
                _pending_plan_task_count(root / "tasks", native_session_id),
                0,
            )

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
