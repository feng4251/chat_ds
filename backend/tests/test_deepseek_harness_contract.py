import json
import re
import sys
import time
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BROWSER_RUNTIME_ROOT = REPOSITORY_ROOT / "executor" / "browser_runtime"
if str(BROWSER_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(BROWSER_RUNTIME_ROOT))

from agent_engines.base import AgentEngineRequest  # noqa: E402
from agent_engines.deepseek_harness import (  # noqa: E402
    DeepSeekEventProjector,
    DeepSeekHarnessEngine,
)
from deepseek_runner.runner_entrypoint import (  # noqa: E402
    NativeEventForwarder,
    _compile_mcp_patch,
)
from routers.chat_router import (  # noqa: E402
    BUILTIN,
    _effective_engine_tools,
    resolve_model_config,
)


def _native(seq, native_type, data=None, *, session="root-native", depth=0):
    return {
        "seq": seq,
        "event": {
            "type": "deepseek.session.event",
            "session_id": session,
            "delegation_depth": depth,
            "session_event": {"type": native_type, "data": data or {}},
        },
    }


def test_native_root_and_child_streams_preserve_authority_boundaries():
    root = "a" * 32
    projector = DeepSeekEventProjector(root)
    started = projector.project(_native(1, "turn/start"))
    assert started[0].data["event_type"] == "run.started"
    assert started[0].data["run_id"] == root

    root_chunk = projector.project(_native(
        2, "assistant/chunk", {"chunk": {"type": "text-delta", "text": "answer"}}
    ))
    assert [(event.kind, event.data.get("delta")) for event in root_chunk] == [
        ("content", "answer")
    ]

    child = projector.project(_native(
        3,
        "assistant/chunk",
        {"chunk": {"type": "text-delta", "text": "worker evidence"}},
        session="renamed-worker-session",
        depth=1,
    ))
    assert [event.kind for event in child] == ["agent_event"]
    assert child[0].data["event_type"] == "run.progress"
    assert child[0].data["payload"]["preview"] == "worker evidence"
    assert child[0].data["parent_run_id"] == root

    native_end = projector.project(_native(
        4, "turn/end", {"reason": {"kind": "completed"}}
    ))
    assert native_end[0].data["event_type"] == "run.progress"
    assert native_end[0].data["payload"]["stage"] == "native_turn_settled"

    terminal = projector.project({
        "seq": 5,
        "event": {"type": "chatds.supervisor.terminal", "status": "succeeded"},
    })
    assert [event.kind for event in terminal] == ["agent_event", "finish"]
    assert terminal[0].data["event_type"] == "run.completed"
    assert terminal[0].data["payload"]["authoritative"] is True


def test_tool_grants_compile_data_driven_and_fail_closed_for_unknown_names():
    requested = [
        "read_file",
        "web_search",
        "delegate_task",
        "warehouse_fixture_only",
        "cronjob",
    ]
    assert _effective_engine_tools("deepseek_harness", requested) == [
        "read_file",
        "web_search",
        "delegate_task",
    ]
    assert _effective_engine_tools("renamed_engine", requested) == requested


@pytest.mark.asyncio
async def test_builtin_model_resolution_preserves_native_engine_bindings():
    """Dispatch resolution must not discard deployment-owned engine authority."""

    for model_id, declared in BUILTIN.items():
        if model_id == "AgentModel":
            continue
        resolved = await resolve_model_config(model_id, object(), None)
        assert resolved.get("claude_provider_profile") == declared.get(
            "claude_provider_profile"
        )
        assert resolved.get("deepseek_harness_provider_profile") == declared.get(
            "deepseek_harness_provider_profile"
        )


def test_engine_payload_binds_only_exact_session_workspace_and_profile():
    engine = DeepSeekHarnessEngine(
        base_url="http://runner.internal",
        internal_token="internal-authority",
        timeout_seconds=60,
    )
    request = AgentEngineRequest(
        run_id="1" * 32,
        root_run_id="1" * 32,
        user_id="2" * 32,
        conversation_id="3" * 32,
        model_id="renamed-model",
        api_model="renamed-api-model",
        messages=({"role": "user", "content": "build an artifact"},),
        max_output_tokens=4096,
        temperature=0,
        provider_config={
            "base_url": "https://provider.invalid/v1",
            "protocol": "openai",
            "context_length": 128_000,
            "deepseek_harness_provider_profile": "deployment-profile",
        },
        tools=("read_file", "write_file"),
        skill_view_path="/immutable/skill-view",
        skill_view_sha256="4" * 64,
        metadata={
            "workspace_path": "/nfs/temp/chat_ds/user-a/session-b/workspace",
            "user_turn_text": "build an artifact",
            "permission_preset": "workspace_write",
        },
    )
    payload = engine._start_payload(request)
    assert payload["workspace_path"].endswith("/user-a/session-b/workspace")
    assert payload["provider_profile"] == "deployment-profile"
    assert payload["tools"] == ["read_file", "write_file"]
    assert "api_key" not in payload


class _RecordingLedger:
    def __init__(self):
        self.rows = []

    def append(self, event, *, channel="controller"):
        self.rows.append((channel, event))


def test_worker_event_spool_is_validated_before_controller_ledger(tmp_path):
    spool = tmp_path / "native-events.jsonl"
    spool.touch()
    ledger = _RecordingLedger()
    forwarder = NativeEventForwarder(spool, ledger)
    forwarder.start()
    event = {
        "event": {
            "type": "deepseek.session.event",
            "session_id": "mutated-child",
            "delegation_depth": 1,
            "session_event": {"type": "turn/start", "data": {}},
        }
    }
    with spool.open("ab") as stream:
        stream.write(json.dumps(event).encode() + b"\n")
        stream.flush()
    deadline = time.monotonic() + 2
    while not ledger.rows and time.monotonic() < deadline:
        time.sleep(0.02)
    forwarder.stop()
    assert ledger.rows == [("deepseek-harness", event["event"])]


def test_worker_event_spool_rejects_incomplete_records(tmp_path):
    spool = tmp_path / "native-events.jsonl"
    spool.write_bytes(b'{"event":')
    forwarder = NativeEventForwarder(spool, _RecordingLedger())
    forwarder.start()
    with pytest.raises(RuntimeError, match="native_event_line_incomplete"):
        forwarder.stop()


def test_mcp_compiler_is_generic_under_cross_domain_rename(tmp_path):
    skill_root = tmp_path / "skill-view"
    plugin = skill_root / "plugin"
    plugin.mkdir(parents=True)
    (plugin / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "warehouse.catalog/v1": {
                "type": "http",
                "url": "https://warehouse.invalid/mcp",
                "headers": {"X-Scope": "read"},
            },
            "factory-feed": {
                "type": "stdio",
                "command": "python",
                "args": ["worker.py"],
                "env": {"MODE": "read"},
            },
            "chatds-web-search": {
                "type": "stdio", "command": "ignored"
            },
            "chatds-schedule": {
                "type": "stdio", "command": "ignored"
            },
        }
    }), encoding="utf-8")
    target = tmp_path / "mcp.patch.json"
    mapping = _compile_mcp_patch(skill_root, target)
    patch = json.loads(target.read_text(encoding="utf-8"))
    assert set(mapping) == {"warehouse.catalog/v1", "factory-feed"}
    assert mapping["factory-feed"] == "factory-feed"
    compiled_id = mapping["warehouse.catalog/v1"]
    assert compiled_id.startswith("warehouse_catalog_v")
    assert len(compiled_id) <= 32
    assert re.search(r"-[0-9a-f]{12}$", compiled_id)
    entries = patch[0]["insert"]
    assert {entry["config"]["transport"] for entry in entries} == {
        "stdio", "streamable-http"
    }
    assert all(entry["name"] == "@deepseek-ai/dsh-mcp-client" for entry in entries)
