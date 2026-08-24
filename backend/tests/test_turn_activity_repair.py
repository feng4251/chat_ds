import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from database import (
    _reconcile_terminal_turn_activity,
    _repair_deepseek_tool_activity_identity,
)
from models import Base


def _native(seq, native_type, data):
    return {
        "seq": seq,
        "event": {
            "type": "deepseek.session.event",
            "session_id": "renamed-museum-worker",
            "delegation_depth": 1,
            "session_event": {"type": native_type, "data": data},
        },
    }


@pytest.mark.asyncio
async def test_native_receipts_repair_one_tool_lifecycle_without_fixture_names():
    with tempfile.TemporaryDirectory() as temp_dir:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(temp_dir) / 'activity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.execute(text(
                    "INSERT INTO users (id, username, hashed_password) VALUES "
                    "('user', 'museum-user', 'test-only-hash')"
                ))
                await connection.execute(text(
                    "INSERT INTO conversations "
                    "(id, user_id, model_id, engine_id, permission_preset, "
                    "workspace_version, goal_started_tokens, input_tokens, "
                    "output_tokens, total_tokens) VALUES "
                    "('conversation', 'user', 'renamed-model', "
                    "'deepseek_harness', 'workspace_write', 1, 0, 0, 0, 0)"
                ))
                await connection.execute(text(
                    "INSERT INTO agent_runs "
                    "(id, user_id, conversation_id, root_run_id, agent_kind, "
                    "depth, workspace_scope, source, engine_id, "
                    "requested_model_id, status, input_tokens, output_tokens, "
                    "total_tokens) VALUES "
                    "('root', 'user', 'conversation', 'root', 'primary', 0, "
                    "'shared_session', 'chat', 'deepseek_harness', "
                    "'renamed-model', 'failed', 0, 0, 0)"
                ))

                started_raw = _native(10, "tool/call", {
                    "callId": "museum-call",
                    "name": "RenamedCatalogLookup",
                })
                completed_raw = _native(11, "tool/result", {
                    "message": {
                        "content": [{
                            "type": "tool-result",
                            "toolCallId": "museum-call",
                            "text": "indexed",
                        }],
                    },
                })
                for raw in (started_raw, completed_raw):
                    serialized = json.dumps(raw)
                    await connection.execute(text(
                        "INSERT INTO agent_engine_raw_events "
                        "(id, user_id, conversation_id, run_id, engine_id, "
                        "seq, payload, payload_sha256) VALUES "
                        "(:id, 'user', 'conversation', 'root', "
                        "'deepseek_harness', :seq, :payload, :digest)"
                    ), {
                        "id": f"raw-{raw['seq']}",
                        "seq": raw["seq"],
                        "payload": serialized,
                        "digest": hashlib.sha256(serialized.encode()).hexdigest(),
                    })

                activities = [
                    (
                        "started",
                        1,
                        "tool:old-start",
                        {"event": {
                            "event_type": "tool.started",
                            "run_id": "worker",
                            "seq": 10_000_000,
                            "payload": {"detail": "started"},
                        }},
                    ),
                    (
                        "completed",
                        2,
                        "tool:old-complete",
                        {"event": {
                            "event_type": "tool.completed",
                            "run_id": "worker",
                            "seq": 11_000_000,
                            "payload": {"detail": "indexed"},
                        }},
                    ),
                ]
                for activity_id, seq, node_id, payload in activities:
                    await connection.execute(text(
                        "INSERT INTO turn_activity_events "
                        "(id, user_id, conversation_id, root_run_id, run_id, "
                        "seq, node_id, kind, operation, payload) VALUES "
                        "(:id, 'user', 'conversation', 'root', 'worker', "
                        ":seq, :node_id, 'tool', 'merge', :payload)"
                    ), {
                        "id": activity_id,
                        "seq": seq,
                        "node_id": node_id,
                        "payload": json.dumps(payload),
                    })

                assert await _repair_deepseek_tool_activity_identity(connection) == 2
                assert await _repair_deepseek_tool_activity_identity(connection) == 0
                rows = (await connection.execute(text(
                    "SELECT node_id, payload FROM turn_activity_events "
                    "ORDER BY seq"
                ))).all()

            expected_node = "tool:" + hashlib.sha256(
                b"museum-call"
            ).hexdigest()[:24]
            assert [row[0] for row in rows] == [expected_node, expected_node]
            for row in rows:
                event = json.loads(row[1])["event"]
                assert event["tool_call_id"] == "museum-call"
                assert event["tool_name"] == "RenamedCatalogLookup"
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_durable_terminals_reconcile_stale_root_and_running_child_cards():
    with tempfile.TemporaryDirectory() as temp_dir:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(temp_dir) / 'terminal-activity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.execute(text(
                    "INSERT INTO users (id, username, hashed_password) VALUES "
                    "('user', 'factory-user', 'test-only-hash')"
                ))
                await connection.execute(text(
                    "INSERT INTO conversations "
                    "(id, user_id, model_id, engine_id, permission_preset, "
                    "workspace_version, goal_started_tokens, input_tokens, "
                    "output_tokens, total_tokens) VALUES "
                    "('conversation', 'user', 'renamed-model', "
                    "'deepseek_harness', 'workspace_write', 1, 0, 0, 0, 0)"
                ))
                await connection.execute(text(
                    "INSERT INTO agent_runs "
                    "(id, user_id, conversation_id, parent_run_id, "
                    "root_run_id, agent_kind, agent_name, depth, "
                    "workspace_scope, source, engine_id, requested_model_id, "
                    "status, finish_reason, error, input_tokens, "
                    "output_tokens, total_tokens) VALUES "
                    "('root', 'user', 'conversation', NULL, 'root', "
                    "'primary', 'Factory coordinator', 0, 'shared_session', "
                    "'chat', 'deepseek_harness', 'renamed-model', 'failed', "
                    "'failed', 'workflow_parse_failed', 0, 0, 0), "
                    "('child', 'user', 'conversation', 'root', 'root', "
                    "'worker', 'Inventory worker', 1, 'shared_session', "
                    "'delegate', 'deepseek_harness', 'renamed-model', "
                    "'cancelled', 'parent_run_terminal', NULL, 0, 0, 0)"
                ))
                await connection.execute(text(
                    "INSERT INTO agent_run_events "
                    "(id, run_id, conversation_id, user_id, parent_run_id, "
                    "seq, event_type, payload) VALUES "
                    "('root-terminal', 'root', 'conversation', 'user', NULL, "
                    "91, 'run.failed', :root_payload), "
                    "('child-terminal', 'child', 'conversation', 'user', "
                    "'root', 17, 'run.cancelled', :child_payload)"
                ), {
                    "root_payload": json.dumps({
                        "authoritative": True,
                        "error": "workflow_parse_failed",
                    }),
                    "child_payload": json.dumps({
                        "authoritative": True,
                        "finish_reason": "parent_run_terminal",
                    }),
                })
                stale_root = {"event": {
                    "event_type": "run.cancelled",
                    "run_id": "root",
                    "root_run_id": "root",
                    "seq": 90,
                    "payload": {"finish_reason": "backend_shutdown"},
                }}
                child_started = {"event": {
                    "event_type": "run.started",
                    "run_id": "child",
                    "root_run_id": "root",
                    "parent_run_id": "root",
                    "seq": 1,
                    "payload": {},
                }}
                await connection.execute(text(
                    "INSERT INTO turn_activity_events "
                    "(id, user_id, conversation_id, root_run_id, run_id, "
                    "seq, node_id, kind, operation, payload) VALUES "
                    "('child-start', 'user', 'conversation', 'root', 'child', "
                    "1, 'workflow:root', 'workflow', 'merge', :child), "
                    "('stale-root', 'user', 'conversation', 'root', 'root', "
                    "2, 'workflow:root', 'workflow', 'merge', :root)"
                ), {
                    "child": json.dumps(child_started),
                    "root": json.dumps(stale_root),
                })

                assert await _reconcile_terminal_turn_activity(connection) == 2
                assert await _reconcile_terminal_turn_activity(connection) == 0
                rows = (await connection.execute(text(
                    "SELECT run_id, payload FROM turn_activity_events "
                    "WHERE kind = 'workflow' ORDER BY seq"
                ))).all()

            terminals = {
                row[0]: json.loads(row[1])["event"]
                for row in rows
                if json.loads(row[1])["event"]["event_type"].startswith("run.")
                and json.loads(row[1])["event"]["event_type"] != "run.started"
            }
            assert terminals["root"]["event_type"] == "run.failed"
            assert terminals["root"]["payload"]["error"] == "workflow_parse_failed"
            assert terminals["child"]["event_type"] == "run.cancelled"
            assert terminals["child"]["parent_run_id"] == "root"
        finally:
            await engine.dispose()
