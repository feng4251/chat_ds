import errno
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_engines.claude_events import ClaudeEventProjector
from agent_engines.claude_code import ClaudeCodeEngine
from agent_engines.base import AgentEngineError
from agent_engines import lifecycle as engine_lifecycle
from agent_engines.skill_view import (
    SkillViewError,
    authorized_skill_sources,
    materialize_claude_skill_view,
)
from config import Settings, settings
from models import (
    AgentEngineRawEvent,
    AgentEngineSession,
    AgentRun,
    Base,
    Conversation,
    Message,
    SkillPackage,
    User,
)
from routers import chat_router, conv_router, workspace_router


class ClaudeEventProjectionTests(unittest.TestCase):
    def test_native_result_is_candidate_until_supervisor_terminal(self):
        projector = ClaudeEventProjector("a" * 32)
        result = projector.project({
            "seq": 1,
            "event": {
                "type": "result",
                "subtype": "success",
                "result": "complete",
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        })
        self.assertNotIn("finish", {event.kind for event in result})
        self.assertFalse(any(
            event.kind == "agent_event"
            and event.data.get("event_type") == "run.completed"
            for event in result
        ))

        terminal = projector.project({
            "seq": 2,
            "event": {
                "type": "chatds.supervisor.terminal",
                "status": "succeeded",
            },
        })
        self.assertEqual(
            [event.data.get("event_type") for event in terminal if event.kind == "agent_event"],
            ["run.completed"],
        )
        self.assertEqual(
            [event.data.get("finish_reason") for event in terminal if event.kind == "finish"],
            ["stop"],
        )

    def test_success_terminal_preserves_pending_control_writes(self):
        projector = ClaudeEventProjector("f" * 32)
        projector.project({
            "seq": 1,
            "event": {"type": "result", "subtype": "success"},
        })
        writes = [{
            "schema": "chatds.schedule-write.v1",
            "operation": "create",
            "tool_call_id": "renamed-receipt",
            "request": {"name": "cross-domain"},
        }]
        events = projector.project({
            "seq": 2,
            "event": {
                "type": "chatds.supervisor.terminal",
                "status": "succeeded",
                "pending_control_writes": writes,
            },
        })
        completed = next(
            event.data for event in events
            if event.kind == "agent_event"
        )
        self.assertEqual(
            completed["payload"]["pending_control_writes"], writes
        )

    def test_success_without_native_result_fails_closed(self):
        projector = ClaudeEventProjector("b" * 32)
        events = projector.project({
            "seq": 1,
            "event": {
                "type": "chatds.supervisor.terminal",
                "status": "succeeded",
            },
        })
        self.assertIn(
            "run.failed",
            [event.data.get("event_type") for event in events],
        )
        self.assertEqual(events[-1].data.get("finish_reason"), "error")

    def test_assistant_fallback_preserves_all_text_and_thinking_blocks(self):
        projector = ClaudeEventProjector("c" * 32)
        events = projector.project({
            "seq": 1,
            "event": {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "r1"},
                        {"type": "thinking", "thinking": "r2"},
                        {"type": "text", "text": "a"},
                        {"type": "text", "text": "b"},
                    ],
                    "usage": {"input_tokens": "bad", "output_tokens": -1},
                },
            },
        })
        self.assertEqual(
            "".join(str(event.data.get("text") or "") for event in events if event.kind == "content"),
            "ab",
        )
        self.assertEqual(
            "".join(str(event.data.get("text") or "") for event in events if event.kind == "reasoning"),
            "r1r2",
        )

    def test_partial_and_full_messages_dedupe_per_message_not_globally(self):
        projector = ClaudeEventProjector("d" * 32)
        partial = projector.project({
            "seq": 1,
            "event": {
                "type": "stream_event",
                "uuid": "message-a",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "first"},
                },
            },
        })
        replay = projector.project({
            "seq": 2,
            "event": {
                "type": "assistant",
                "uuid": "message-a",
                "message": {"content": [{"type": "text", "text": "first"}]},
            },
        })
        later = projector.project({
            "seq": 3,
            "event": {
                "type": "assistant",
                "uuid": "message-b",
                "message": {"content": [{"type": "text", "text": "second"}]},
            },
        })
        self.assertEqual([event.data["text"] for event in partial], ["first"])
        self.assertFalse(any(event.kind == "content" for event in replay))
        self.assertEqual(
            [event.data["text"] for event in later if event.kind == "content"],
            ["second"],
        )

    def test_partial_and_full_message_dedupe_uses_native_message_id(self):
        projector = ClaudeEventProjector("d" * 32)
        projector.project({
            "seq": 1,
            "event": {
                "type": "stream_event",
                "uuid": "stream-start-uuid",
                "parent_tool_use_id": None,
                "event": {
                    "type": "message_start",
                    "message": {
                        "id": "native-message-id",
                        "model": "fixture",
                    },
                },
            },
        })
        partial = projector.project({
            "seq": 2,
            "event": {
                "type": "stream_event",
                "uuid": "delta-has-a-different-uuid",
                "parent_tool_use_id": None,
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "once"},
                },
            },
        })
        full = projector.project({
            "seq": 3,
            "event": {
                "type": "assistant",
                "uuid": "assistant-has-another-uuid",
                "parent_tool_use_id": None,
                "message": {
                    "id": "native-message-id",
                    "content": [{"type": "text", "text": "once"}],
                },
            },
        })
        self.assertEqual(
            [
                event.data["text"]
                for event in partial
                if event.kind == "content"
            ],
            ["once"],
        )
        self.assertFalse(any(event.kind == "content" for event in full))

    def test_tool_start_is_deduplicated_between_partial_and_full_events(self):
        projector = ClaudeEventProjector("e" * 32)
        partial = projector.project({
            "seq": 1,
            "event": {
                "type": "stream_event",
                "uuid": "message-tool",
                "event": {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                    },
                },
            },
        })
        full = projector.project({
            "seq": 2,
            "event": {
                "type": "assistant",
                "uuid": "message-tool",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Bash",
                }]},
            },
        })
        self.assertEqual(
            sum(event.data.get("event_type") == "tool.started" for event in partial),
            1,
        )
        self.assertFalse(any(
            event.data.get("event_type") == "tool.started" for event in full
        ))
        terminal = projector.project({
            "seq": 3,
            "event": {
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "is_error": True,
                }]},
            },
        })
        completed = next(
            event
            for event in terminal
            if event.data.get("event_type") == "tool.failed"
        )
        self.assertEqual(completed.data["tool_name"], "Bash")

    def test_native_task_identity_routes_child_tools_and_stopped_terminal(self):
        root = "f" * 32
        projector = ClaudeEventProjector(root)
        started = projector.project({
            "seq": 1,
            "event": {
                "type": "system",
                "subtype": "task_started",
                "task_id": "native-task",
                "tool_use_id": "agent-tool",
                "description": "Safety evidence extraction",
            },
        })
        child_events = [
            event for event in started if event.kind == "agent_event"
        ]
        self.assertEqual(
            [event.data["event_type"] for event in child_events],
            ["agent.spawned", "run.started"],
        )
        child_run_id = child_events[0].data["run_id"]
        child_tool = projector.project({
            "seq": 2,
            "event": {
                "type": "assistant",
                "parent_tool_use_id": "agent-tool",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "child-tool",
                    "name": "Read",
                }]},
            },
        })
        self.assertEqual(
            [
                event.data["run_id"]
                for event in child_tool
                if event.data.get("event_type") == "tool.started"
            ],
            [child_run_id],
        )
        stopped = projector.project({
            "seq": 3,
            "event": {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "native-task",
                "status": "stopped",
                "summary": "Stopped by parent",
            },
        })
        self.assertEqual(stopped[0].data["event_type"], "run.cancelled")
        self.assertEqual(stopped[0].data["run_id"], child_run_id)

    def test_background_shell_is_not_projected_as_a_delegate_agent(self):
        projector = ClaudeEventProjector("7" * 32)
        started = projector.project({
            "event": {
                "type": "system",
                "subtype": "task_started",
                "task_id": "bash-task",
                "task_type": "local_bash",
                "description": "watch output",
            },
        })
        self.assertEqual([event.kind for event in started], ["tool_progress"])
        self.assertFalse(any(
            event.data.get("event_type") in {"agent.spawned", "run.started"}
            for event in started
        ))
        completed = projector.project({
            "event": {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "bash-task",
                "status": "completed",
            },
        })
        self.assertEqual([event.kind for event in completed], ["tool_progress"])

    def test_controller_terminal_exposes_safe_stage_and_code(self):
        projector = ClaudeEventProjector("6" * 32)
        events = projector.project({
            "event": {
                "type": "chatds.supervisor.terminal",
                "status": "failed",
                "error": "RuntimeError",
                "error_code": "artifact_contract_audit_failed",
                "error_stage": "artifact_contract_audit",
                "native_task_summary": {"task_count": 1},
            },
        })
        diagnostic = next(event for event in events if event.kind == "diagnostic")
        self.assertEqual(
            diagnostic.data["error_code"],
            "artifact_contract_audit_failed",
        )
        self.assertEqual(
            diagnostic.data["error_stage"],
            "artifact_contract_audit",
        )

    def test_workspace_artifact_is_projected_to_durable_artifact_event(self):
        root = "8" * 32
        projector = ClaudeEventProjector(root)
        events = projector.project({
            "seq": 1,
            "event": {
                "type": "chatds.workspace.artifact",
                "kind": "file",
                "title": "report.md",
                "path": "results/report.md",
                "size_bytes": 123,
                "sha256": "a" * 64,
                "source_event_key": "claude-workspace:key",
            },
        })
        artifact = self.assert_single_agent_event(
            events,
            "artifact.created",
        )
        self.assertEqual(artifact["run_id"], root)
        self.assertEqual(artifact["tool_name"], "ClaudeCodeWorkspace")
        self.assertEqual(artifact["payload"], {
            "kind": "file",
            "title": "report.md",
            "path": "results/report.md",
            "size_bytes": 123,
            "sha256": "a" * 64,
            "source_event_key": "claude-workspace:key",
        })

    def test_result_error_preserves_native_error_list(self):
        projector = ClaudeEventProjector("9" * 32)
        projector.project({
            "seq": 1,
            "event": {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "errors": ["provider disconnected", "retry exhausted"],
            },
        })
        terminal = projector.project({
            "seq": 2,
            "event": {
                "type": "chatds.supervisor.terminal",
                "status": "failed",
            },
        })
        message = next(
            event.data["message"]
            for event in terminal
            if event.kind == "diagnostic"
        )
        self.assertIn("provider disconnected", message)
        self.assertIn("retry exhausted", message)

    def assert_single_agent_event(self, events, event_type):
        matches = [
            event.data
            for event in events
            if event.kind == "agent_event"
            and event.data.get("event_type") == event_type
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]


class ClaudeSkillViewTests(unittest.TestCase):
    def test_generic_artifact_and_runtime_contracts_are_compiled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "archive-synthesis-renamed"
            (skill / "orchestration").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "Keep a persistent JSONL process alive and write requests to stdin.\n"
                "Allow anti-bot bypass, but never conceal webdriver automation signals.\n",
                encoding="utf-8",
            )
            (skill / "orchestration" / "pipeline.yaml").write_text(
                """final_report_template:
  auto_merge:
    command_template: "cat 01_*.md 02_*.md > {PROJECT}_FULL_REPORT.md"
    output_artifact: "{PROJECT}_FULL_REPORT.md"
    expected_size_range: "10KB-20KB"
    post_merge_verification:
      - "Line count > 42"
""",
                encoding="utf-8",
            )
            view = materialize_claude_skill_view(
                session_root=root / "session",
                sources=[SimpleNamespace(
                    name="archive-synthesis-renamed",
                    scope="session",
                    root=skill,
                    bundle_id=None,
                    bundle_role=None,
                )],
            )
            manifest = json.loads((view.root / "manifest.json").read_text())
            mcp = json.loads((view.plugin_root / ".mcp.json").read_text())
        self.assertEqual(manifest["artifact_contracts"], [{
            "skill_name": "archive-synthesis-renamed",
            "declared_final_artifact": "{PROJECT}_FULL_REPORT.md",
            "declared_modular_files": ["01_*.md", "02_*.md"],
            "expected_min_bytes": 10 * 1024,
            "expected_max_bytes": 20 * 1024,
            "expected_min_lines": 42,
        }])
        self.assertEqual(manifest["runtime_requirements"], [{
            "skill_name": "archive-synthesis-renamed",
            "persistent_stdin_process": True,
        }])
        self.assertEqual(
            manifest["skill_diagnostics"][0]["code"],
            "contradictory_automation_evasion_policy",
        )
        self.assertEqual(set(mcp["mcpServers"]), {"chatds-process"})
        self.assertEqual(
            mcp["mcpServers"]["chatds-process"]["args"],
            ["-I", "-m", "claude_runner.mcp_process"],
        )

    def test_size_checks_cannot_be_miscompiled_as_line_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "warehouse-audit"
            (skill / "orchestration").mkdir(parents=True)
            (skill / "SKILL.md").write_text("Audit inventory.", encoding="utf-8")
            (skill / "orchestration" / "contract.yaml").write_text(
                """final_report_template:
  auto_merge:
    output_artifact: "{WAREHOUSE}_AUDIT.md"
    expected_size_range: "2KB-8KB"
    post_merge_verification:
      - "File size > 1KB"
      - "Line count > 73"
""",
                encoding="utf-8",
            )
            view = materialize_claude_skill_view(
                session_root=root / "session",
                sources=[SimpleNamespace(
                    name="warehouse-audit",
                    scope="session",
                    root=skill,
                    bundle_id=None,
                    bundle_role=None,
                )],
            )
            manifest = json.loads((view.root / "manifest.json").read_text())
        contract = manifest["artifact_contracts"][0]
        self.assertEqual(contract["expected_min_bytes"], 2 * 1024)
        self.assertEqual(contract["expected_min_lines"], 73)

    def test_conflicting_structured_artifact_authority_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "satellite-review"
            (skill / "orchestration").mkdir(parents=True)
            (skill / "SKILL.md").write_text("Review readiness.", encoding="utf-8")
            for name, artifact in (
                ("alpha.yaml", "ALPHA.md"),
                ("beta.yaml", "BETA.md"),
            ):
                (skill / "orchestration" / name).write_text(
                    "output_contract:\n"
                    f"  final_artifact: {artifact}\n",
                    encoding="utf-8",
                )
            with self.assertRaisesRegex(
                SkillViewError, "skill_output_contract_conflict"
            ):
                materialize_claude_skill_view(
                    session_root=root / "session",
                    sources=[SimpleNamespace(
                        name="satellite-review",
                        scope="session",
                        root=skill,
                        bundle_id=None,
                        bundle_role=None,
                    )],
                )

    def test_session_scope_wins_and_executable_resources_remain_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skills = root / "skills"
            user_id = "1" * 32
            session_id = "2" * 32
            user_skill = skills / user_id / "fixture"
            session_skill = skills / user_id / session_id / "fixture"
            for directory, marker in ((user_skill, "user"), (session_skill, "session")):
                (directory / "scripts").mkdir(parents=True)
                (directory / "SKILL.md").write_text(marker, encoding="utf-8")
                script = directory / "scripts" / "run.sh"
                script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                script.chmod(0o755)
            rows = [
                SimpleNamespace(
                    name="fixture", session_id=None, bundle_id=None, bundle_role=None
                ),
                SimpleNamespace(
                    name="fixture",
                    session_id=session_id,
                    bundle_id=None,
                    bundle_role=None,
                ),
            ]
            sources = authorized_skill_sources(
                user_id=user_id,
                session_id=session_id,
                registry_rows=rows,
                skills_data_dir=skills,
            )
            self.assertEqual([(source.name, source.scope) for source in sources], [
                ("fixture", "session")
            ])
            view = materialize_claude_skill_view(
                session_root=root / "workspaces" / user_id / session_id,
                sources=sources,
            )
            published = view.plugin_root / "skills" / "fixture"
            self.assertEqual((published / "SKILL.md").read_text(), "session")
            self.assertTrue(os.stat(published / "scripts" / "run.sh").st_mode & 0o111)
            self.assertFalse(os.stat(view.root).st_mode & 0o222)
            manifest = json.loads((view.root / "manifest.json").read_text())
            self.assertEqual(manifest["sha256"], view.sha256)
            self.assertIsNone(view.entrypoint_skill_name)
            self.assertEqual(view.selected_primary_skill_names, ("fixture",))
            self.assertEqual(
                manifest["selected_primary_skill_names"],
                ["fixture"],
            )
            self.assertFalse(
                (
                    view.plugin_root
                    / "skills"
                    / "chatds-harness-session-entry"
                ).exists()
            )

    def test_bundle_supporting_skills_are_not_promoted_to_entrypoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "skills" / "primary"
            supporting = root / "skills" / "supporting"
            for skill in (primary, supporting):
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    skill.name,
                    encoding="utf-8",
                )
            view = materialize_claude_skill_view(
                session_root=root / "session",
                sources=[
                    SimpleNamespace(
                        name="primary",
                        scope="session",
                        root=primary,
                        bundle_id="bundle",
                        bundle_role="primary",
                    ),
                    SimpleNamespace(
                        name="supporting",
                        scope="session",
                        root=supporting,
                        bundle_id="bundle",
                        bundle_role="supporting",
                    ),
                ],
            )
            self.assertEqual(
                view.selected_primary_skill_names,
                ("primary",),
            )
            self.assertIsNone(view.entrypoint_skill_name)
            self.assertFalse(
                (
                    view.plugin_root
                    / "skills"
                    / "chatds-harness-session-entry"
                ).exists()
            )

    def test_enabled_web_search_compiles_one_harness_owned_mcp_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "renamed-holdout"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "Use only when a museum provenance request applies.",
                encoding="utf-8",
            )
            source = SimpleNamespace(
                name="renamed-holdout",
                scope="session",
                root=skill,
                bundle_id=None,
                bundle_role=None,
            )
            enabled = materialize_claude_skill_view(
                session_root=root / "enabled",
                sources=[source],
                enabled_tools=["read_file", "web_search"],
                web_search_url="http://search.internal:8080/search",
            )
            disabled = materialize_claude_skill_view(
                session_root=root / "disabled",
                sources=[source],
                enabled_tools=["read_file"],
            )
            enabled_mcp = json.loads(
                (enabled.plugin_root / ".mcp.json").read_text()
            )
            disabled_mcp = json.loads(
                (disabled.plugin_root / ".mcp.json").read_text()
            )
            manifest = json.loads(
                (enabled.root / "manifest.json").read_text()
            )
        self.assertEqual(
            set(enabled_mcp["mcpServers"]),
            {"chatds-web-search"},
        )
        self.assertEqual(
            enabled_mcp["mcpServers"]["chatds-web-search"]["args"],
            ["-I", "-m", "claude_runner.mcp_web_search"],
        )
        self.assertEqual(disabled_mcp, {"mcpServers": {}})
        self.assertEqual(manifest["harness_egress_rules"], [{
            "capability": "web_search",
            "url_prefix": "http://search.internal:8080/search",
            "methods": ["GET"],
        }])

    def test_cronjob_compiles_backend_owned_schedule_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "renamed-holdout"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "Use only for factory equipment requests.", encoding="utf-8"
            )
            view = materialize_claude_skill_view(
                session_root=root / "session",
                sources=[SimpleNamespace(
                    name="renamed-holdout",
                    scope="session",
                    root=skill,
                    bundle_id=None,
                    bundle_role=None,
                )],
                enabled_tools=["cronjob"],
            )
            mcp = json.loads((view.plugin_root / ".mcp.json").read_text())
            manifest = json.loads((view.root / "manifest.json").read_text())

        self.assertEqual(set(mcp["mcpServers"]), {"chatds-schedule"})
        self.assertEqual(
            mcp["mcpServers"]["chatds-schedule"]["args"],
            ["-I", "-m", "claude_runner.mcp_schedule_control"],
        )
        self.assertEqual(manifest["harness_capabilities"], ["schedule_control"])
        self.assertEqual(manifest["harness_egress_rules"], [])

    def test_market_quote_compiles_typed_gateway_not_public_provider_origins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "renamed-holdout"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "Use only for museum provenance requests.",
                encoding="utf-8",
            )
            view = materialize_claude_skill_view(
                session_root=root / "session",
                sources=[SimpleNamespace(
                    name="renamed-holdout",
                    scope="session",
                    root=skill,
                    bundle_id=None,
                    bundle_role=None,
                )],
                enabled_tools=["market_quote"],
                market_data_url="http://market-data.internal:8090/v1/quote",
            )
            mcp = json.loads((view.plugin_root / ".mcp.json").read_text())
            manifest = json.loads((view.root / "manifest.json").read_text())
        self.assertEqual(set(mcp["mcpServers"]), {"chatds-market-data"})
        config = mcp["mcpServers"]["chatds-market-data"]
        self.assertEqual(
            config["args"],
            ["-I", "-m", "claude_runner.mcp_market_data"],
        )
        self.assertEqual(
            config["env"]["CHATDS_MARKET_DATA_URL"],
            "http://market-data.internal:8090/v1/quote",
        )
        self.assertEqual(manifest["harness_egress_rules"], [{
            "capability": "market_quote",
            "url_prefix": "http://market-data.internal:8090/v1/quote",
            "methods": ["GET"],
        }])
        serialized = json.dumps(manifest)
        self.assertNotIn("sinajs", serialized)
        self.assertNotIn("gtimg", serialized)
        self.assertNotIn("eastmoney", serialized)

    def test_harness_entrypoint_name_is_reserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "reserved"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("fixture", encoding="utf-8")
            with self.assertRaisesRegex(SkillViewError, "reserved"):
                materialize_claude_skill_view(
                    session_root=root / "session",
                    sources=[SimpleNamespace(
                        name="chatds-harness-session-entry",
                        scope="session",
                        root=skill,
                        bundle_id=None,
                        bundle_role=None,
                    )],
                )

    def test_content_addressed_view_reuses_nfs_enotempty_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "fixture"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("fixture", encoding="utf-8")
            source = SimpleNamespace(
                name="fixture",
                scope="session",
                root=skill,
                bundle_id=None,
                bundle_role=None,
            )
            winner = materialize_claude_skill_view(
                session_root=root / "session",
                sources=[source],
            )
            with patch.object(
                Path,
                "rename",
                side_effect=OSError(
                    errno.ENOTEMPTY,
                    "Directory not empty",
                ),
            ):
                reused = materialize_claude_skill_view(
                    session_root=root / "session",
                    sources=[source],
                )
            self.assertEqual(reused.sha256, winner.sha256)
            self.assertEqual(reused.root, winner.root)

    def test_explicit_skill_mcp_is_compiled_for_isolated_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "fixture"
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text("fixture", encoding="utf-8")
            (skill / "scripts" / "server.py").write_text(
                "print('fixture')\n", encoding="utf-8"
            )
            (skill / ".mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "local-db": {
                        "command": "python3",
                        "args": [str(skill / "scripts" / "server.py")],
                    },
                    "remote-db": {
                        "type": "http",
                        "url": "https://mcp.example.test/v1/mcp",
                    },
                },
            }), encoding="utf-8")
            source = SimpleNamespace(
                name="fixture",
                scope="session",
                root=skill,
                bundle_id=None,
                bundle_role=None,
            )
            view = materialize_claude_skill_view(
                session_root=root / "session",
                sources=[source],
            )
            config = json.loads(
                (view.plugin_root / ".mcp.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                config["mcpServers"]["local-db"]["args"],
                ["/skill-view/plugin/skills/fixture/scripts/server.py"],
            )
            self.assertEqual(
                config["mcpServers"]["remote-db"]["type"], "http"
            )
            self.assertEqual(
                view.mcp_server_names, ("local-db", "remote-db")
            )

    def test_mcp_helpers_that_can_escape_policy_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "fixture"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("fixture", encoding="utf-8")
            (skill / ".mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://mcp.example.test/v1/mcp",
                        "headersHelper": "steal-credentials",
                    },
                },
            }), encoding="utf-8")
            source = SimpleNamespace(
                name="fixture",
                scope="session",
                root=skill,
                bundle_id=None,
                bundle_role=None,
            )
            with self.assertRaises(SkillViewError):
                materialize_claude_skill_view(
                    session_root=root / "session",
                    sources=[source],
                )

    def test_symlinked_skill_resource_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "u" / "fixture"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("fixture", encoding="utf-8")
            (skill / "escape").symlink_to(root)
            source = SimpleNamespace(
                name="fixture", scope="user", root=skill, bundle_id=None, bundle_role=None
            )
            with self.assertRaises(SkillViewError):
                materialize_claude_skill_view(
                    session_root=root / "session",
                    sources=[source],
                )


class EngineModelCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "test.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add(User(
                id="1" * 32,
                username="engine-model-fixture",
                hashed_password="fixture",
            ))
            await db.commit()
        self.user = SimpleNamespace(id="1" * 32)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temporary.cleanup()

    async def test_claude_engine_exposes_only_deployment_profile_models(self):
        with patch.object(settings, "claude_code_engine_enabled", True):
            async with self.sessions() as db:
                options = await workspace_router._engine_options_for_user(
                    current_model_id="deepseek_v4_pro",
                    user=self.user,
                    db=db,
                )
        claude = next(item for item in options if item["id"] == "claude_code")
        self.assertEqual(
            claude["compatible_model_ids"],
            ["shaiengine_glm_5_2", "shaiengine_deepseek_v4_pro"],
        )
        self.assertEqual(claude["default_model_id"], "shaiengine_glm_5_2")
        self.assertNotIn("deepseek_v4_pro", claude["compatible_model_ids"])

    async def test_explicit_local_profiles_expose_only_their_bound_models(self):
        profiles = [
            "shaiengine",
            "local_agentmodel",
            "local_deepseek_v4_flash",
            "local_qwen",
        ]
        with (
            patch.object(settings, "claude_code_engine_enabled", True),
            patch.object(settings, "claude_code_provider_profiles", profiles),
        ):
            async with self.sessions() as db:
                options = await workspace_router._engine_options_for_user(
                    current_model_id="deepseek_v4_pro",
                    user=self.user,
                    db=db,
                )
        claude = next(item for item in options if item["id"] == "claude_code")
        self.assertEqual(claude["compatible_model_ids"], [
            "shaiengine_glm_5_2",
            "shaiengine_deepseek_v4_pro",
            "deepseek_v4_pro",
            "local_deepseek_v4_flash",
            "qwen3_5",
        ])
        self.assertEqual(claude["default_model_id"], "deepseek_v4_pro")

    async def test_legacy_engine_can_be_retained_for_history_only(self):
        with patch.object(settings, "legacy_engine_new_runs_enabled", False):
            async with self.sessions() as db:
                options = await workspace_router._engine_options_for_user(
                    current_model_id="deepseek_v4_pro",
                    user=self.user,
                    db=db,
                )
        legacy = next(item for item in options if item["id"] == "legacy")
        self.assertFalse(legacy["available"])
        self.assertIn("history", legacy["unavailable_reason"])

    def test_provider_family_without_explicit_profile_is_not_compatible(self):
        with patch.object(
            settings,
            "claude_code_provider_profiles",
            [
                "shaiengine",
                "local_agentmodel",
                "local_deepseek_v4_flash",
                "local_qwen",
            ],
        ):
            self.assertFalse(chat_router.claude_code_model_compatible({
                "provider": "builtin",
            }))
            self.assertTrue(chat_router.claude_code_model_compatible({
                "provider": "builtin",
                "claude_provider_profile": "local_agentmodel",
            }))

    def test_colliding_wire_model_names_keep_distinct_route_capabilities(self):
        local_glm = chat_router.BUILTIN["deepseek_v4_pro"]
        local_deepseek = chat_router.BUILTIN["local_deepseek_v4_flash"]
        self.assertEqual(local_glm["api_model"], local_deepseek["api_model"])
        self.assertNotEqual(local_glm["base_url"], local_deepseek["base_url"])
        self.assertNotEqual(
            local_glm["claude_provider_profile"],
            local_deepseek["claude_provider_profile"],
        )
        self.assertEqual(local_glm["context_length"], 918_528)
        self.assertEqual(local_deepseek["context_length"], 1_048_576)

    async def test_new_session_uses_configured_engine_without_rebinding_existing(self):
        async with self.sessions() as db:
            existing = Conversation(
                user_id=self.user.id,
                model_id="shaiengine_glm_5_2",
                engine_id="legacy",
            )
            db.add(existing)
            await db.commit()
            with (
                patch.object(settings, "default_agent_engine_id", "claude_code"),
                patch.object(
                    conv_router,
                    "ensure_workspace_async",
                    AsyncMock(),
                ),
                patch.object(conv_router, "emit_event", AsyncMock()),
            ):
                created = await conv_router.create_conversation(
                    cur_user=self.user,
                    db=db,
                )
            await db.refresh(existing)
        self.assertEqual(created["engine_id"], "claude_code")
        self.assertEqual(existing.engine_id, "legacy")

    def test_claude_default_fails_closed_when_engine_is_disabled(self):
        with self.assertRaisesRegex(
            ValueError,
            "CLAUDE_CODE_ENGINE_ENABLED=true",
        ):
            Settings(
                _env_file=None,
                claude_code_engine_enabled=False,
                default_agent_engine_id="claude_code",
            )

    def test_disabled_legacy_cannot_remain_the_default_engine(self):
        with self.assertRaisesRegex(
            ValueError,
            "LEGACY_ENGINE_NEW_RUNS_ENABLED=true",
        ):
            Settings(
                _env_file=None,
                claude_code_engine_enabled=True,
                legacy_engine_new_runs_enabled=False,
                default_agent_engine_id="legacy",
            )

    def test_claude_payload_uses_explicit_profile_and_omits_caller_secret(self):
        engine = ClaudeCodeEngine(
            base_url="http://runner.test",
            internal_token="fixture-internal-token",
            timeout_seconds=60,
        )
        request = SimpleNamespace(
            run_id="1" * 32,
            root_run_id="1" * 32,
            user_id="2" * 32,
            conversation_id="3" * 32,
            model_id="deepseek_v4_pro",
            api_model="AgentModel",
            provider_config={
                "provider": "builtin",
                "claude_provider_profile": "local_agentmodel",
                "base_url": "http://10.10.132.2:1025/v1",
                "protocol": "openai",
                "context_length": 303872,
                "api_key": "must-not-cross-the-engine-boundary",
            },
            messages=({"role": "user", "content": "fixture"},),
            max_output_tokens=8,
            metadata={"workspace_path": "/workspace", "user_turn_text": "fixture"},
            skill_view_path="/skill-view",
            skill_view_sha256="a" * 64,
            native_session_id="4" * 32,
            resume_from_native_session_id=None,
            source="chat",
        )
        payload = engine._start_payload(request)
        self.assertEqual(payload["provider_profile"], "local_agentmodel")
        self.assertEqual(payload["context_window_tokens"], 303872)
        self.assertNotIn("api_key", payload)
        self.assertNotIn("must-not-cross", json.dumps(payload))

    def test_claude_payload_rejects_unbound_model_capacity(self):
        engine = ClaudeCodeEngine(
            base_url="http://runner.test",
            internal_token="fixture-internal-token",
            timeout_seconds=60,
        )
        request = SimpleNamespace(
            provider_config={"context_length": True},
        )
        with self.assertRaises(AgentEngineError) as raised:
            engine._start_payload(request)
        self.assertEqual(
            raised.exception.code,
            "claude_model_capability_invalid",
        )
        self.assertFalse(raised.exception.retryable)

    async def test_claude_start_timeout_is_not_misreported_as_stream_timeout(self):
        class StartTimeoutClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                raise httpx.ReadTimeout("start fixture")

        engine = ClaudeCodeEngine(
            base_url="http://runner.test",
            internal_token="fixture-internal-token",
            timeout_seconds=60,
            client_factory=StartTimeoutClient,
        )
        request = SimpleNamespace(
            run_id="1" * 32,
            root_run_id="1" * 32,
            user_id="2" * 32,
            conversation_id="3" * 32,
            model_id="shaiengine_glm_5_2",
            api_model="glm-5.2",
            provider_config={
                "claude_provider_profile": "shaiengine",
                "base_url": "https://api.shaiengine.com/v1",
                "protocol": "openai",
                "context_length": 1_000_000,
            },
            messages=({"role": "user", "content": "fixture"},),
            max_output_tokens=8,
            metadata={"workspace_path": "/workspace", "user_turn_text": "fixture"},
            skill_view_path="/skill-view",
            skill_view_sha256="a" * 64,
            native_session_id=str(uuid.uuid4()),
            resume_from_native_session_id=None,
            source="chat",
        )
        with self.assertRaises(AgentEngineError) as raised:
            async for _event in engine.stream(request):
                pass
        self.assertEqual(raised.exception.code, "claude_runner_start_timeout")
        self.assertTrue(raised.exception.retryable)

    async def test_backend_models_do_not_disappear_when_harness_is_unavailable(self):
        with patch.object(
            chat_router.httpx,
            "AsyncClient",
            side_effect=RuntimeError("offline fixture"),
        ):
            async with self.sessions() as db:
                result = await chat_router.get_models(cur_user=self.user, db=db)
        identifiers = {item["id"] for item in result["models"]}
        self.assertIn("shaiengine_glm_5_2", identifiers)
        self.assertIn("shaiengine_deepseek_v4_pro", identifiers)
        self.assertIn("deepseek_v4_pro", identifiers)

    async def test_claude_chat_entry_materializes_session_skill_before_run(self):
        conversation_id = "6" * 32
        skills_root = Path(self.temporary.name) / "skills"
        session_root = Path(self.temporary.name) / "sessions" / conversation_id
        skill_root = (
            skills_root
            / str(self.user.id)
            / conversation_id
            / "fixture-skill"
        )
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "---\nname: fixture-skill\ndescription: route fixture\n---\n",
            encoding="utf-8",
        )
        async with self.sessions() as db:
            db.add(Conversation(
                id=conversation_id,
                user_id=str(self.user.id),
                model_id="shaiengine_glm_5_2",
                engine_id="claude_code",
            ))
            db.add(SkillPackage(
                user_id=str(self.user.id),
                session_id=conversation_id,
                name="fixture-skill",
            ))
            await db.commit()

        def discard_detached(*, operation, **_kwargs):
            operation.close()
            return None

        def discard_best_effort(operation, **_kwargs):
            operation.close()
            return None

        lease = await chat_router._acquire_conversation_turn(conversation_id)
        try:
            with (
                patch.object(
                    chat_router,
                    "resolve_model_config",
                    AsyncMock(return_value={
                        "id": "shaiengine_glm_5_2",
                        "api_model": "glm-5.2",
                        "provider": "builtin",
                        "claude_provider_profile": "shaiengine",
                        "base_url": "https://provider.invalid/v1",
                        "api_key": "fixture",
                        "context_length": 1_000_000,
                    }),
                ),
                patch.object(
                    chat_router,
                    "claude_code_model_compatible",
                    return_value=True,
                ),
                patch(
                    "routers.skill_router.SKILLS_DATA_DIR",
                    skills_root,
                ),
                patch.object(
                    chat_router.workspace_store,
                    "session_root",
                    return_value=session_root,
                ),
                patch.object(
                    chat_router,
                    "_track_detached_chat_producer",
                    side_effect=discard_detached,
                ),
                patch.object(
                    chat_router,
                    "_track_best_effort_task",
                    side_effect=discard_best_effort,
                ),
            ):
                async with self.sessions() as db:
                    conversation = await db.get(Conversation, conversation_id)
                    response = await chat_router._chat_stream_with_turn(
                        chat_router.ChatRequest(
                            conversation_id=conversation_id,
                            content="exercise the Claude route",
                            model_id="shaiengine_glm_5_2",
                            engine_id="claude_code",
                        ),
                        self.user,
                        db,
                        conv=conversation,
                        conv_id=conversation_id,
                        model_id="shaiengine_glm_5_2",
                        turn_lease=lease,
                    )
            self.assertEqual(response.media_type, "text/event-stream")
        finally:
            chat_router._release_conversation_turn(conversation_id, lease)

        async with self.sessions() as db:
            engine_session = (await db.execute(
                select(AgentEngineSession).where(
                    AgentEngineSession.conversation_id == conversation_id
                )
            )).scalar_one()
            run = (await db.execute(
                select(AgentRun).where(
                    AgentRun.conversation_id == conversation_id
                )
            )).scalar_one()
            message = (await db.execute(
                select(Message).where(
                    Message.conversation_id == conversation_id
                )
            )).scalar_one()
        self.assertEqual(engine_session.status, "running")
        self.assertEqual(engine_session.active_run_id, run.id)
        self.assertEqual(engine_session.engine_id, "claude_code")
        self.assertEqual(run.engine_id, "claude_code")
        self.assertEqual(message.content, "exercise the Claude route")
        self.assertTrue(engine_session.skill_view_sha256)
        self.assertTrue(
            (
                session_root
                / "runtime"
                / "claude"
                / "skill-views"
                / engine_session.skill_view_sha256
                / "plugin"
                / "skills"
                / "fixture-skill"
                / "SKILL.md"
            ).is_file()
        )


class NativeCheckpointCommitBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "test.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.user_id = "3" * 32
        self.conversation_id = "4" * 32
        async with self.sessions() as db:
            db.add(User(
                id=self.user_id,
                username="checkpoint-fixture",
                hashed_password="fixture",
            ))
            db.add(Conversation(
                id=self.conversation_id,
                user_id=self.user_id,
                model_id="shaiengine_glm_5_2",
                engine_id="claude_code",
            ))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temporary.cleanup()

    async def _seed_success(self, *, with_raw_terminal: bool):
        run_id = "5" * 32
        candidate = str(uuid.uuid4())
        async with self.sessions() as db:
            db.add(AgentRun(
                id=run_id,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                root_run_id=run_id,
                engine_id="claude_code",
                native_session_id=candidate,
                requested_model_id="shaiengine_glm_5_2",
                status="succeeded",
            ))
            db.add(AgentEngineSession(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                engine_id="claude_code",
                status="running",
                active_run_id=run_id,
            ))
            if with_raw_terminal:
                envelope = {
                    "seq": 7,
                    "channel": "controller",
                    "event": {
                        "type": "chatds.supervisor.terminal",
                        "status": "succeeded",
                        "result_succeeded": True,
                        "checkpoint_observed": True,
                    },
                }
                payload = json.dumps(envelope, separators=(",", ":"))
                db.add(AgentEngineRawEvent(
                    user_id=self.user_id,
                    conversation_id=self.conversation_id,
                    run_id=run_id,
                    engine_id="claude_code",
                    seq=7,
                    native_event_type="chatds.supervisor.terminal",
                    payload=payload,
                    payload_sha256=hashlib.sha256(
                        payload.encode()
                    ).hexdigest(),
                ))
            await db.commit()
        return run_id, candidate

    async def test_success_promotes_only_after_lossless_terminal_is_durable(self):
        run_id, candidate = await self._seed_success(with_raw_terminal=True)
        with patch.object(chat_router, "async_session", self.sessions):
            await chat_router._finalize_native_engine_session(
                run_id, self.conversation_id
            )
        async with self.sessions() as db:
            state = (await db.execute(select(AgentEngineSession))).scalar_one()
        self.assertEqual(state.status, "idle")
        self.assertEqual(state.native_session_id, candidate)
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.last_event_seq, 7)

    async def test_success_without_lossless_terminal_is_failed_closed(self):
        run_id, _candidate = await self._seed_success(with_raw_terminal=False)
        with patch.object(chat_router, "async_session", self.sessions):
            with self.assertRaisesRegex(
                RuntimeError, "claude_native_terminal_audit_incomplete"
            ):
                await chat_router._finalize_native_engine_session(
                    run_id, self.conversation_id
                )
        async with self.sessions() as db:
            run = await db.get(AgentRun, run_id)
            state = (await db.execute(select(AgentEngineSession))).scalar_one()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.finish_reason, "native_audit_incomplete")
        self.assertEqual(state.status, "failed")
        self.assertIsNone(state.native_session_id)

    async def test_failed_outer_contract_keeps_complete_transcript_checkpoint(self):
        run_id = "7" * 32
        candidate = "28c25fd9-a780-47f4-a8a2-8d233e5fd263"
        envelope = {
            "seq": 9,
            "channel": "controller",
            "event": {
                "type": "chatds.supervisor.terminal",
                "status": "failed",
                "error_code": "artifact_contract_failed",
                "result_succeeded": True,
                "checkpoint_observed": True,
            },
        }
        payload = json.dumps(envelope, separators=(",", ":"))
        async with self.sessions() as db:
            db.add(AgentRun(
                id=run_id,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                engine_id="claude_code",
                native_session_id=candidate,
                requested_model_id="renamed-cross-domain-model",
                status="failed",
                error="artifact_contract_failed",
            ))
            db.add(AgentEngineSession(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                engine_id="claude_code",
                status="running",
                active_run_id=run_id,
            ))
            db.add(AgentEngineRawEvent(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                run_id=run_id,
                engine_id="claude_code",
                seq=9,
                native_event_type="chatds.supervisor.terminal",
                payload=payload,
                payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
            ))
            await db.commit()
        with patch.object(chat_router, "async_session", self.sessions):
            await chat_router._finalize_native_engine_session(
                run_id, self.conversation_id
            )
        async with self.sessions() as db:
            state = (await db.execute(select(AgentEngineSession))).scalar_one()
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.native_session_id, candidate)
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.last_event_seq, 9)

    async def test_startup_promotes_only_unique_lossless_success(self):
        run_id, candidate = await self._seed_success(with_raw_terminal=True)
        cancel = AsyncMock(return_value=True)
        registry = SimpleNamespace(get=lambda _engine_id: SimpleNamespace(
            cancel_run=cancel,
        ))
        with (
            patch.object(engine_lifecycle, "async_session", self.sessions),
            patch.object(
                engine_lifecycle.settings,
                "claude_code_engine_enabled",
                True,
            ),
            patch.object(
                engine_lifecycle,
                "build_agent_engine_registry",
                return_value=registry,
            ),
        ):
            revoked = await engine_lifecycle.revoke_stale_native_runs_on_backend_startup()
        self.assertEqual(revoked, 0)
        cancel.assert_not_awaited()
        async with self.sessions() as db:
            state = (await db.execute(select(AgentEngineSession))).scalar_one()
        self.assertEqual(state.status, "idle")
        self.assertEqual(state.native_session_id, candidate)
        self.assertEqual(state.generation, 1)

    async def test_startup_revokes_projected_success_with_missing_raw_terminal(self):
        run_id, _candidate = await self._seed_success(with_raw_terminal=False)
        cancel = AsyncMock(return_value=True)
        registry = SimpleNamespace(get=lambda _engine_id: SimpleNamespace(
            cancel_run=cancel,
        ))
        with (
            patch.object(engine_lifecycle, "async_session", self.sessions),
            patch.object(
                engine_lifecycle.settings,
                "claude_code_engine_enabled",
                True,
            ),
            patch.object(
                engine_lifecycle,
                "build_agent_engine_registry",
                return_value=registry,
            ),
        ):
            revoked = await engine_lifecycle.revoke_stale_native_runs_on_backend_startup()
        self.assertEqual(revoked, 1)
        cancel.assert_awaited_once()
        async with self.sessions() as db:
            run = await db.get(AgentRun, run_id)
            state = (await db.execute(select(AgentEngineSession))).scalar_one()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.finish_reason, "native_audit_incomplete")
        self.assertEqual(state.status, "failed")
        self.assertIsNone(state.native_session_id)

    async def test_raw_native_ledger_is_idempotent_and_rejects_conflicts(self):
        run_id, _candidate = await self._seed_success(with_raw_terminal=False)
        envelope = {
            "seq": 11,
            "channel": "stdout",
            "event": {"type": "system", "subtype": "init", "uuid": "event-11"},
        }
        with patch.object(chat_router, "async_session", self.sessions):
            await chat_router._persist_engine_raw_events(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                run_id=run_id,
                engine_id="claude_code",
                envelopes=[envelope, dict(envelope)],
            )
            await chat_router._persist_engine_raw_events(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                run_id=run_id,
                engine_id="claude_code",
                envelopes=[envelope],
            )
            conflicting = {
                "seq": 11,
                "channel": "stdout",
                "event": {"type": "system", "subtype": "changed"},
            }
            with self.assertRaisesRegex(
                RuntimeError, "sequence replay changed payload"
            ):
                await chat_router._persist_engine_raw_events(
                    user_id=self.user_id,
                    conversation_id=self.conversation_id,
                    run_id=run_id,
                    engine_id="claude_code",
                    envelopes=[conflicting],
                )
        async with self.sessions() as db:
            rows = list((await db.execute(
                select(AgentEngineRawEvent).where(
                    AgentEngineRawEvent.run_id == run_id,
                    AgentEngineRawEvent.seq == 11,
                )
            )).scalars().all())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].native_event_id, "event-11")

    async def test_large_native_event_persists_a_content_addressed_pointer(self):
        run_id, _candidate = await self._seed_success(with_raw_terminal=False)
        envelope = {
            "seq": 12,
            "channel": "stdout",
            "event": {
                "type": "assistant",
                "message": "x" * (chat_router._RAW_ENGINE_EVENT_INLINE_BYTES + 1024),
            },
        }
        canonical = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with patch.object(chat_router, "async_session", self.sessions):
            await chat_router._persist_engine_raw_events(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                run_id=run_id,
                engine_id="claude_code",
                envelopes=[envelope],
            )
        async with self.sessions() as db:
            row = (await db.execute(
                select(AgentEngineRawEvent).where(
                    AgentEngineRawEvent.run_id == run_id,
                    AgentEngineRawEvent.seq == 12,
                )
            )).scalar_one()
        pointer = json.loads(row.payload)
        self.assertFalse(pointer["inline"])
        self.assertEqual(pointer["storage"], "claude_runner_session_ledger")
        self.assertEqual(pointer["payload_sha256"], expected_sha)
        self.assertEqual(row.payload_sha256, expected_sha)
        self.assertEqual(pointer["size_bytes"], len(canonical.encode("utf-8")))


if __name__ == "__main__":
    unittest.main()
