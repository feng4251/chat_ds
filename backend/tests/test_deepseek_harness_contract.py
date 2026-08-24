import json
import os
import re
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BROWSER_RUNTIME_ROOT = REPOSITORY_ROOT / "executor" / "browser_runtime"
if str(BROWSER_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(BROWSER_RUNTIME_ROOT))

from agent_engines.base import AgentEngineRequest  # noqa: E402
from agent_engines.deepseek_harness import (  # noqa: E402
    DeepSeekEventProjector,
    DeepSeekHarnessEngine,
    deepseek_native_tool_identity,
    deepseek_tool_result_call_id,
)
from deepseek_runner.runner_entrypoint import (  # noqa: E402
    ControlDecisionForwarder,
    NativeEventReceiver,
    _compile_mcp_patch,
    _environment,
    _native_command,
    _native_turn_payload,
    _terminal_error_stage,
    _terminal_outcome,
)
from deepseek_runner.terminal_receipts import (  # noqa: E402
    append_terminal,
    terminal_status,
)
from deepseek_runner.control_decisions import (  # noqa: E402
    answerable_control_request,
    append_control_decision,
    approval_request_seq,
    existing_control_decision,
    lower_native_question_answer,
)
from deepseek_runner.config import (  # noqa: E402
    DEFAULT_EGRESS_LIMITS,
    state_volume_host_root,
)
from deepseek_runner.event_stream import read_event_tail  # noqa: E402
from deepseek_runner.native_session import native_turn_prompts  # noqa: E402
from native_tools import deepseek_harness_native_tools  # noqa: E402
from native_security.workflow_contract import (  # noqa: E402
    compile_turn_workflow_contract,
)
from deepseek_runner.native_workflow import (  # noqa: E402
    build_deepseek_workflow_receipt,
    compile_deepseek_workflow_projection,
)
from deepseek_runner.native_artifacts import (  # noqa: E402
    active_artifact_skill_names,
    successful_root_skill_invocations,
)
from routers.chat_router import (  # noqa: E402
    BUILTIN,
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


def test_native_engine_default_budget_admits_long_context_provider_exchange():
    assert DEFAULT_EGRESS_LIMITS["max_outbound_bytes"] == 1024 * 1024 * 1024


def test_native_root_and_child_streams_preserve_authority_boundaries():
    root = "a" * 32
    projector = DeepSeekEventProjector(root)
    started = projector.project(_native(1, "turn/start"))
    assert started[0].data["event_type"] == "run.started"
    assert started[0].data["run_id"] == root

    root_chunk = projector.project(_native(
        2, "assistant/chunk", {"chunk": {"type": "text-delta", "text": "answer"}}
    ))
    assert [(event.kind, event.data.get("text")) for event in root_chunk] == [
        ("content", "answer")
    ]

    root_reasoning = projector.project(_native(
        3,
        "assistant/chunk",
        {"chunk": {"type": "reasoning-delta", "text": "inspect evidence"}},
    ))
    assert [(event.kind, event.data.get("text")) for event in root_reasoning] == [
        ("reasoning", "inspect evidence")
    ]

    child = projector.project(_native(
        4,
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
        5, "turn/end", {"reason": {"kind": "completed"}}
    ))
    assert native_end[0].data["event_type"] == "run.progress"
    assert native_end[0].data["payload"]["stage"] == "native_turn_settled"

    terminal = projector.project({
        "seq": 6,
        "event": {"type": "chatds.supervisor.terminal", "status": "succeeded"},
    })
    assert [event.kind for event in terminal] == ["agent_event", "finish"]
    assert terminal[0].data["event_type"] == "run.completed"
    assert terminal[0].data["payload"]["authoritative"] is True


def test_native_approval_audit_is_not_a_second_actionable_card():
    projector = DeepSeekEventProjector("a" * 32)
    audited = projector.project(_native(
        7,
        "approval/asked",
        {"id": "native-approval", "toolName": "renamed_write", "callId": "call-1"},
    ))
    assert [event.kind for event in audited] == ["diagnostic"]

    requested = projector.project(_native(
        8,
        "chatds/approval/requested",
        {
            "request_id": "native-approval",
            "tool_name": "renamed_write",
            "call_id": "call-1",
        },
    ))
    assert [event.kind for event in requested] == ["approval"]
    assert requested[0].data["request_id"] == "native-approval"
    assert requested[0].data["status"] == "pending"

    decided = projector.project(_native(
        9,
        "approval/decided",
        {"id": "native-approval", "outcome": "allowed-once"},
    ))
    assert [event.kind for event in decided] == ["approval"]
    assert decided[0].data == {
        "request_id": "native-approval",
        "request_seq": 9_000_000,
        "status": "allowed",
    }


def test_native_tool_result_reuses_call_identity_across_upstream_shapes():
    projector = DeepSeekEventProjector("f" * 32)
    started = projector.project(_native(
        20,
        "tool/call",
        {"callId": "museum-call", "name": "RenamedMuseumLookup"},
    ))
    completed_envelope = _native(
        21,
        "tool/result",
        {
            "message": {
                "content": [{
                    "type": "tool-result",
                    "toolCallId": "museum-call",
                    "text": "indexed",
                }],
                "source": {"callId": "museum-call"},
            },
        },
    )
    completed = projector.project(completed_envelope)

    assert started[0].data["payload"]["tool_call_id"] == "museum-call"
    assert completed[0].data["payload"]["tool_call_id"] == "museum-call"
    assert completed[0].data["payload"]["tool_name"] == "RenamedMuseumLookup"
    assert deepseek_tool_result_call_id(
        {"source": {"callId": "museum-call"}}
    ) == "museum-call"
    assert deepseek_native_tool_identity(completed_envelope) == (
        "tool/result",
        "museum-call",
        "",
    )


def test_native_plan_review_projects_one_binary_web_decision():
    projector = DeepSeekEventProjector("a" * 32)
    requested = projector.project(_native(
        10,
        "chatds/question/requested",
        {
            "request_id": "question-renamed-plan",
            "question_id": "review",
            "question": "Approve this warehouse plan?",
            "detail": "# Warehouse plan\n\n- Verify receipts",
            "intent_kind": "plan-review",
            "intent_approve": "Ship warehouse plan",
            "options": [
                {"label": "Keep planning"},
                {"label": "Ship warehouse plan"},
            ],
        },
    ))
    assert [event.kind for event in requested] == ["approval"]
    assert requested[0].data["request_id"] == "question-renamed-plan"
    assert requested[0].data["request_seq"] == 10_000_000
    assert requested[0].data["tool_name"] == "exit_plan_mode"


def test_native_question_projection_and_lowering_survive_domain_rename():
    projector = DeepSeekEventProjector("b" * 32)
    requested = projector.project(_native(
        11,
        "chatds/question/requested",
        {
            "request_id": "museum-question",
            "question_id": "wing",
            "question": "Which museum wings should be audited?",
            "header": "Wings",
            "multi_select": True,
            "options": [
                {
                    "label": "East",
                    "description": "Audit the east wing",
                    "preview": "private curator material",
                },
                {"label": "West", "description": "Audit the west wing"},
            ],
            "private_input": "must-not-project",
        },
    ))
    assert [event.kind for event in requested] == ["approval"]
    data = requested[0].data
    assert data["interaction_kind"] == "question"
    assert data["questions"][0]["multi_select"] is True
    assert "preview" not in data["questions"][0]["options"][0]
    assert "must-not-project" not in str(data)

    selected, custom = lower_native_question_answer(
        {
            "question": "Which museum wings should be audited?",
            "multi_select": True,
            "options": [{"label": "East"}, {"label": "West"}],
        },
        "East, West",
    )
    assert selected == ["East", "West"]
    assert custom is None

    mutated = projector.project(_native(
        12,
        "chatds/question/requested",
        {
            "request_id": "factory-question",
            "question": "Which factory line should be audited?",
            "options": [
                {"label": "Line A"},
                {"label": "Line A"},
            ],
        },
    ))
    assert mutated == ()

    decided = projector.project(_native(
        13,
        "chatds/question/decided",
        {
            "request_id": "museum-question",
            "decision": "allow",
            "tool_name": "ask_user_question",
            "interaction_kind": "question",
        },
    ))
    assert decided[0].data["status"] == "allowed"
    assert decided[0].data["interaction_kind"] == "question"


def test_native_tool_graph_is_not_compiled_from_chatds_capability_names():
    warehouse_catalog = deepseek_harness_native_tools()
    renamed_laboratory_catalog = deepseek_harness_native_tools()
    assert warehouse_catalog == renamed_laboratory_catalog
    assert {"read", "write", "subagent", "workflow", "ralph"} <= set(
        warehouse_catalog
    )
    assert "read_file" not in warehouse_catalog
    assert "delegate_task" not in warehouse_catalog

    patch_text = (
        REPOSITORY_ROOT / "deepseek_runner" / "chatds.patch.yml"
    ).read_text(encoding="utf-8")
    assert "CHATDS_DSH_FILES_ENABLED" not in patch_text
    assert "CHATDS_DSH_SUBAGENTS_ENABLED" not in patch_text
    assert "id: tool-ask-user" in patch_text
    assert "id: tool-ralph\n  disabled: true" not in patch_text
    assert "\n- id: permission\n" not in patch_text
    assert (
        "policy: !!js \"process.env.DSH_PERMISSION_MODE === "
        "'danger-full-access' ? 'never' : 'ask'\""
    ) in patch_text


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
        native_session_id=f"chatds-{'3' * 32}",
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
    assert payload["native_session_id"] == f"chatds-{'3' * 32}"
    assert "tools" not in payload
    assert "api_key" not in payload


@pytest.mark.asyncio
async def test_engine_recovers_only_the_supervisor_owned_terminal_receipt():
    terminal = {
        "seq": 91,
        "channel": "supervisor",
        "event": {
            "type": "chatds.supervisor.terminal",
            "status": "failed",
            "error": "renamed_workflow_failure",
        },
    }

    async def handler(request):
        assert request.url.path.endswith(f"/v1/runs/{'1' * 32}/terminal")
        assert json.loads(request.content) == {
            "user_id": "2" * 32,
            "conversation_id": "3" * 32,
        }
        return httpx.Response(200, json={"terminal": terminal, "last_seq": 91})

    transport = httpx.MockTransport(handler)
    engine = DeepSeekHarnessEngine(
        base_url="http://runner.internal",
        internal_token="internal-authority",
        timeout_seconds=60,
        client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=transport, **kwargs
        ),
    )
    recovered = await engine.recover_terminal(
        user_id="2" * 32,
        conversation_id="3" * 32,
        run_id="1" * 32,
    )
    assert recovered == terminal


def test_daemon_volume_mountpoint_is_used_for_dynamic_run_bind(tmp_path):
    class Volume:
        attrs = {
            "Name": "renamed-state-volume",
            "Driver": "local",
            "Mountpoint": str(tmp_path / "daemon-volume-data"),
        }

    class Volumes:
        @staticmethod
        def get(name):
            assert name == "renamed-state-volume"
            return Volume()

    class Client:
        volumes = Volumes()

    root = state_volume_host_root(Client(), "renamed-state-volume")
    assert root == tmp_path / "daemon-volume-data"
    assert str(root).startswith(str(tmp_path))


def test_native_headless_command_satisfies_upstream_loader_contract(tmp_path):
    command = _native_command(tmp_path / "mcp.json")
    assert command[:4] == [
        "/usr/local/bin/node",
        "--expose-internals",
        "--use-env-proxy",
        "/opt/deepseek-harness/apps/cli/lib/bin.js",
    ]
    assert command[-1] == "chatds-native-turn"
    assert "renamed cross-domain task" not in command


def test_native_session_inputs_import_once_then_preserve_exact_turn():
    messages = [
        {"role": "system", "content": "ChatDS-owned control text"},
        {"role": "user", "content": "Inspect the museum ledger"},
        {"role": "assistant", "content": "The north wing is indexed"},
        {"role": "user", "content": "Continue with the renamed gallery"},
    ]
    initial, current = native_turn_prompts(
        messages,
        "Continue with the renamed gallery",
    )
    assert "ChatDS-owned control text" not in initial
    assert "Inspect the museum ledger" in initial
    assert current == "Continue with the renamed gallery"
    assert native_turn_prompts(
        [{"role": "user", "content": "Audit the factory line"}],
        "Audit the factory line",
    ) == ("Audit the factory line", "Audit the factory line")


def test_native_turn_payload_is_exact_and_rejects_identity_mutation():
    config = {
        "native_session_id": f"chatds-{'7' * 32}",
        "permission_preset": "workspace_write",
        "initial_prompt": "Inspect a renamed warehouse",
        "turn_prompt": "Continue the audit",
        "controller_only": "must-not-cross",
    }
    payload = json.loads(_native_turn_payload(config))
    assert payload == {
        "schema": "chatds.deepseek-native-turn.v1",
        "native_session_id": f"chatds-{'7' * 32}",
        "permission_preset": "read-only",
        "initial_prompt": "Inspect a renamed warehouse",
        "turn_prompt": "Continue the audit",
    }
    with pytest.raises(RuntimeError, match="native_turn_input_invalid"):
        _native_turn_payload({**config, "native_session_id": "fixture-session"})
    with pytest.raises(RuntimeError, match="deepseek_permission_preset_invalid"):
        _native_turn_payload({**config, "permission_preset": "fixture_access"})


def test_native_worker_environment_is_explicit_and_session_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_HARNESS_PROVIDER_API_KEY", "test-only-provider-key")
    environment = _environment(
        {
            "permission_preset": "workspace_write",
            "tools": ["read_file", "web_search"],
            "provider_base_url": "https://provider.invalid/v1",
            "api_model": "renamed-model",
            "context_window_tokens": 128_000,
            "max_output_tokens": 8_192,
            "web_search_enabled": True,
            "searxng_search_url": "http://search.invalid/search",
        },
        proxy_url="http://127.0.0.1:43123",
        trust={"SSL_CERT_FILE": str(tmp_path / "ca.pem")},
        worker_tmp=tmp_path,
    )
    assert environment["HOME"] == "/state/home"
    assert environment["DSH_HOME"] == "/state/dsh"
    assert environment["DSH_PERMISSION_MODE"] == "read-only"
    assert environment["CHATDS_WEB_PERMISSION_PRESET"] == "workspace_write"
    assert environment["DSH_TOOLS_MODE"] == "native"
    assert environment["CHATDS_DSH_MODEL"] == "renamed-model"
    assert environment["DEEPSEEK_API_KEY"] == "test-only-provider-key"
    assert environment["CHATDS_DSH_TURN_INPUT"] == (
        "/runtime/controller/native-turn.json"
    )
    assert not any(
        key.startswith("CHATDS_DSH_") and key.endswith("_ENABLED")
        for key in environment
    )


@pytest.mark.parametrize(("preset", "native_mode"), [
    ("read_only", "read-only"),
    ("workspace_write", "read-only"),
    ("session_full", "danger-full-access"),
])
def test_web_permission_tiers_map_to_native_baselines_and_exact_browser_policy(
    monkeypatch,
    tmp_path,
    preset,
    native_mode,
):
    monkeypatch.setenv("DEEPSEEK_HARNESS_PROVIDER_API_KEY", "test-only-provider-key")
    environment = _environment(
        {
            "permission_preset": preset,
            "provider_base_url": "https://provider.invalid/v1",
            "api_model": "renamed-model",
            "context_window_tokens": 128_000,
            "max_output_tokens": 8_192,
            "searxng_search_url": "http://search.invalid/search",
        },
        proxy_url="http://127.0.0.1:43123",
        trust={},
        worker_tmp=tmp_path,
    )
    assert environment["DSH_PERMISSION_MODE"] == native_mode
    assert environment["CHATDS_WEB_PERMISSION_PRESET"] == preset


def test_native_immutable_inputs_are_separated_from_worker_mailboxes(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DEEPSEEK_HARNESS_PROVIDER_API_KEY", "test-only-provider-key")
    environment = _environment(
        {
            "permission_preset": "workspace_write",
            "provider_base_url": "https://provider.invalid/v1",
            "api_model": "renamed-model",
            "context_window_tokens": 128_000,
            "max_output_tokens": 8_192,
            "searxng_search_url": "http://search.invalid/search",
            "workflow_projection": {"skill_name": "museum-catalog"},
            "artifact_contracts": [{"skill_name": "museum-catalog"}],
        },
        proxy_url="http://127.0.0.1:43123",
        trust={},
        worker_tmp=tmp_path,
    )
    assert environment["CHATDS_DSH_TURN_INPUT"].startswith(
        "/runtime/controller/"
    )
    assert environment["CHATDS_DSH_WORKFLOW_PROJECTION"].startswith(
        "/runtime/controller/"
    )
    assert environment["CHATDS_DSH_ARTIFACT_PROJECTION"].startswith(
        "/runtime/controller/"
    )
    assert environment["CHATDS_EVENT_SOCKET"].startswith("/runtime/controller/")
    assert "CHATDS_EVENT_LEDGER" not in environment


class _RecordingLedger:
    def __init__(self):
        self.rows = []

    def append(self, event, *, channel="controller"):
        self.rows.append((channel, event))


def test_native_event_socket_accepts_only_authorized_harness_pid(tmp_path):
    endpoint = tmp_path / "native-events.sock"
    ledger = _RecordingLedger()
    receiver = NativeEventReceiver(endpoint, ledger, worker_gid=os.getgid())
    receiver.start()
    receiver.authorize_pid(os.getpid())
    event = {
        "type": "deepseek.session.event",
        "session_id": "renamed-root",
        "delegation_depth": 0,
        "session_event": {"type": "turn/start", "data": {}},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(endpoint))
        client.sendall(json.dumps(event).encode() + b"\n")
    deadline = time.monotonic() + 2
    while not ledger.rows and time.monotonic() < deadline:
        time.sleep(0.02)
    receiver.stop()
    assert ledger.rows == [("deepseek-harness", event)]


def test_native_event_socket_rejects_wrong_process_identity(tmp_path):
    endpoint = tmp_path / "native-events.sock"
    receiver = NativeEventReceiver(
        endpoint,
        _RecordingLedger(),
        worker_gid=os.getgid(),
    )
    receiver.start()
    receiver.authorize_pid(os.getpid() + 1_000_000)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(endpoint))
        client.sendall(b'{"type":"deepseek.session.event"}\n')
    with pytest.raises(RuntimeError, match="native_event_peer_invalid"):
        receiver.stop()


def test_supervisor_fallback_terminal_preserves_failure_stage(tmp_path):
    events = tmp_path / "events.jsonl"
    append_terminal(
        events,
        "failed",
        "renamed_preflight_failure",
        error_stage="skill_attestation",
    )
    envelope = json.loads(events.read_text())
    assert envelope["event"] == {
        "type": "chatds.supervisor.terminal",
        "status": "failed",
        "error": "renamed_preflight_failure",
        "error_stage": "skill_attestation",
    }


def test_terminal_receipts_scan_large_renamed_ledger_without_read_text(
    tmp_path, monkeypatch,
):
    events = tmp_path / "warehouse-events.jsonl"
    with events.open("wb") as stream:
        for seq in range(1, 20_001):
            stream.write(json.dumps({
                "seq": seq,
                "event": {"type": "warehouse.inventory", "row": seq},
            }, separators=(",", ":")).encode() + b"\n")

    def forbidden_read_text(*_args, **_kwargs):
        raise AssertionError("durable ledgers must be scanned incrementally")

    monkeypatch.setattr(Path, "read_text", forbidden_read_text)
    assert terminal_status(events) is None
    append_terminal(
        events,
        "failed",
        "renamed_reconciliation_failure",
        error_stage="terminal_reconciliation",
    )
    append_terminal(events, "cancelled", None)
    assert terminal_status(events) == "failed"
    with events.open("rb") as stream:
        rows = [json.loads(line) for line in stream]
    terminals = [
        row for row in rows
        if row.get("event", {}).get("type") == "chatds.supervisor.terminal"
    ]
    assert len(terminals) == 1
    assert terminals[0]["seq"] == 20_001


def test_event_replay_reads_a_large_ledger_in_bounded_batches(tmp_path):
    events = tmp_path / "renamed-replay.jsonl"
    payload = "inventory-" + ("x" * 480)
    with events.open("wb") as stream:
        for seq in range(1, 5_001):
            stream.write(json.dumps({
                "seq": seq,
                "event": {"type": "warehouse.inventory", "payload": payload},
            }, separators=(",", ":")).encode() + b"\n")
    file_size = events.stat().st_size
    position, first = read_event_tail(events, 0)
    assert first
    assert position < file_size
    rows = list(first)
    while position < file_size:
        next_position, batch = read_event_tail(events, position)
        assert next_position > position
        position = next_position
        rows.extend(batch)
    assert len(rows) == 5_000
    assert json.loads(rows[-1])["seq"] == 5_000


def test_control_decision_forwarder_is_complete_validated_and_lossless(tmp_path):
    mailbox = tmp_path / "mailbox.jsonl"
    worker = tmp_path / "worker" / "decisions.jsonl"
    mailbox.touch()
    forwarder = ControlDecisionForwarder(mailbox, worker)
    forwarder.start()
    row = {
        "request_id": "renamed-approval",
        "request_seq": 17_000_000,
        "decision": "allow",
    }
    with mailbox.open("ab") as stream:
        stream.write(json.dumps(row).encode() + b"\n")
        stream.flush()
    deadline = time.monotonic() + 2
    while (not worker.exists() or not worker.read_bytes()) and time.monotonic() < deadline:
        time.sleep(0.02)
    forwarder.stop()
    assert json.loads(worker.read_text()) == row


def test_control_decision_forwarder_surfaces_malformed_mailbox(tmp_path):
    mailbox = tmp_path / "mailbox.jsonl"
    worker = tmp_path / "worker" / "decisions.jsonl"
    mailbox.write_text('{"request_id":"unterminated"', encoding="utf-8")
    forwarder = ControlDecisionForwarder(mailbox, worker)
    forwarder.start()
    with pytest.raises(RuntimeError, match="control_decision_line_incomplete"):
        forwarder.stop()


def test_supervisor_approval_receipt_binds_sequence_and_is_idempotent(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({
        "seq": 23,
        "event": {
            "type": "deepseek.session.event",
            "delegation_depth": 0,
            "session_event": {
                "type": "chatds/approval/requested",
                "data": {"request_id": "museum-approval"},
            },
        },
    }) + "\n", encoding="utf-8")
    assert approval_request_seq(events, "museum-approval") == 23_000_000
    assert approval_request_seq(events, "other-approval") is None

    mailbox = tmp_path / "control-decisions.jsonl"
    decision = {
        "request_id": "museum-approval",
        "request_seq": 23_000_000,
        "decision": "deny",
    }
    append_control_decision(mailbox, decision)
    assert existing_control_decision(mailbox, "museum-approval") == decision

    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "seq": 24,
            "event": {
                "type": "deepseek.session.event",
                "delegation_depth": 0,
                "session_event": {
                    "type": "chatds/question/requested",
                    "data": {
                        "request_id": "warehouse-plan-review",
                        "intent_kind": "plan-review",
                        "intent_approve": "Ship warehouse plan",
                    },
                },
            },
        }) + "\n")
    assert approval_request_seq(events, "warehouse-plan-review") == 24_000_000

    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "seq": 25,
            "event": {
                "type": "deepseek.session.event",
                "delegation_depth": 0,
                "session_event": {
                    "type": "chatds/question/requested",
                    "data": {
                        "request_id": "factory-question",
                        "question": "Which production line?",
                        "options": [
                            {"label": "Line A"},
                            {"label": "Line B"},
                        ],
                    },
                },
            },
        }) + "\n")
    receipt = answerable_control_request(events, "factory-question")
    assert receipt is not None
    assert receipt[0] == 25_000_000
    assert receipt[1]["data"]["question"] == "Which production line?"


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


def _workflow_manifest(*, skill_name, route_id, worker_ids, pattern):
    worker_agents = []
    files = []
    for index, worker_id in enumerate(worker_ids):
        source_path = f"orchestration/workers/{worker_id}.yaml"
        digest = f"{index + 1:064x}"
        worker_agents.append({
            "skill_name": skill_name,
            "worker_id": worker_id,
            "source_path": source_path,
            "agent_path": f"plugin/agents/{skill_name}/{worker_id}.md",
            "native_agent_type": (
                f"chatds-session-skills:{skill_name}:{worker_id}"
            ),
        })
        files.append({"path": source_path, "sha256": digest, "size": 100 + index})
    phases = [
        {
            "mode": "parallel",
            "workers": [
                {
                    "worker_id": worker_id,
                    "native_agent_type": (
                        f"chatds-session-skills:{skill_name}:{worker_id}"
                    ),
                }
                for worker_id in worker_ids[:-1]
            ],
        },
        {
            "mode": "sequential",
            "workers": [{
                "worker_id": worker_ids[-1],
                "native_agent_type": (
                    f"chatds-session-skills:{skill_name}:{worker_ids[-1]}"
                ),
            }],
        },
    ]
    return {
        "selected_primary_skill_names": [skill_name],
        "skills": [{
            "name": skill_name,
            "scope": "session",
            "bundle_role": "primary",
            "files": files,
        }],
        "worker_agents": worker_agents,
        "workflow_routes": [{
            "skill_name": skill_name,
            "route_id": route_id,
            "source_path": "orchestration/orchestrator.yaml",
            "priority": 25,
            "patterns": [pattern],
            "phases": phases,
        }],
    }


def _workflow_event(seq, session_id, native_type, data):
    return {
        "seq": seq,
        "event": {
            "type": "deepseek.session.event",
            "session_id": session_id,
            "delegation_depth": 0,
            "session_event": {
                "seq": seq + 100,
                "type": native_type,
                "data": data,
            },
        },
    }


def _root_tool_event(seq, session_id, native_type, data):
    return {
        "seq": seq,
        "event": {
            "type": "deepseek.session.event",
            "session_id": session_id,
            "delegation_depth": 0,
            "session_event": {
                "seq": seq + 100,
                "type": native_type,
                "data": data,
            },
        },
    }


def test_native_skill_activation_requires_successful_root_tool_receipts():
    native_session_id = f"chatds-{'7' * 32}"
    events = [
        _root_tool_event(1, native_session_id, "tool/call", {
            "callId": "warehouse-call",
            "name": "skill",
            "arguments": json.dumps({"name": "warehouse-audit"}),
        }),
        _root_tool_event(2, native_session_id, "tool/result", {
            "message": {
                "source": {"kind": "tool", "callId": "warehouse-call"},
                "content": [{
                    "type": "tool-result",
                    "toolCallId": "warehouse-call",
                    "isError": False,
                    "content": [{"type": "text", "text": "loaded"}],
                }],
            },
        }),
        # A child may load supporting Skills; that does not activate a root
        # artifact contract for the current Turn.
        {
            **_root_tool_event(3, "child-session", "tool/call", {
                "callId": "child-call",
                "name": "skill",
                "arguments": json.dumps({"name": "child-only-skill"}),
            }),
            "event": {
                **_root_tool_event(3, "child-session", "tool/call", {
                    "callId": "child-call",
                    "name": "skill",
                    "arguments": json.dumps({"name": "child-only-skill"}),
                })["event"],
                "delegation_depth": 1,
            },
        },
        # Failed loads do not activate a contract.
        _root_tool_event(4, native_session_id, "tool/call", {
            "callId": "failed-call",
            "name": "skill",
            "arguments": json.dumps({"name": "museum-catalog"}),
        }),
        _root_tool_event(5, native_session_id, "tool/result", {
            "message": {
                "source": {"kind": "tool", "callId": "failed-call"},
                "content": [{
                    "type": "tool-result",
                    "toolCallId": "failed-call",
                    "isError": True,
                    "content": [{"type": "text", "text": "unavailable"}],
                }],
            },
        }),
        # Code Mode has a distinct durable pair and is equally authoritative.
        _root_tool_event(6, native_session_id, "tool/code-dispatch-start", {
            "rootCallId": "code-root",
            "parentCallId": "code-root",
            "subCallId": "code-root:code:1",
            "name": "skill",
            "arguments": {"name": "factory-inspection"},
        }),
        _root_tool_event(7, native_session_id, "tool/code-dispatch", {
            "rootCallId": "code-root",
            "parentCallId": "code-root",
            "subCallId": "code-root:code:1",
            "name": "skill",
            "arguments": {"name": "factory-inspection"},
            "isError": False,
            "content": [{"type": "text", "text": "loaded"}],
        }),
    ]
    assert successful_root_skill_invocations(
        events, native_session_id=native_session_id
    ) == ("factory-inspection", "warehouse-audit")
    assert active_artifact_skill_names(
        contracts=[
            {"skill_name": "warehouse-audit"},
            {"skill_name": "factory-inspection"},
            {"skill_name": "museum-catalog"},
        ],
        workflow_projection=None,
        envelopes=events,
        native_session_id=native_session_id,
    ) == ("factory-inspection", "warehouse-audit")


def test_workflow_binding_activates_only_its_exact_artifact_contract():
    native_session_id = f"chatds-{'6' * 32}"
    assert active_artifact_skill_names(
        contracts=[
            {"skill_name": "renamed-laboratory"},
            {"skill_name": "unrelated-skill"},
        ],
        workflow_projection={"skill_name": "renamed-laboratory"},
        envelopes=[],
        native_session_id=native_session_id,
    ) == ("renamed-laboratory",)


def test_terminal_outcome_fails_closed_on_artifact_receipt():
    assert _terminal_outcome(
        exit_code=0,
        stop_reason=None,
        workflow_passed=True,
        artifact_passed=False,
    ) == ("failed", "artifact_contract_failed")
    assert _terminal_outcome(
        exit_code=0,
        stop_reason=None,
        workflow_passed=True,
        artifact_passed=True,
    ) == ("succeeded", None)


def test_terminal_failure_stage_names_the_failed_receipt_not_later_cleanup():
    assert _terminal_error_stage(
        status="failed",
        terminal_error="workflow_contract_failed",
        current_stage="egress_bridge_seal",
    ) == "workflow_contract_audit"
    assert _terminal_error_stage(
        status="failed",
        terminal_error="artifact_contract_failed",
        current_stage="artifact_contract_audit",
    ) == "artifact_contract_audit"
    assert _terminal_error_stage(
        status="failed",
        terminal_error="runner_exit_nonzero",
        current_stage="egress_bridge_seal",
    ) == "native_execution"
    assert _terminal_error_stage(
        status="succeeded",
        terminal_error=None,
        current_stage="artifact_contract_audit",
    ) is None


def test_deepseek_workflow_projection_survives_cross_domain_rename():
    cases = [
        (
            "warehouse-audit",
            "full-ledger-audit",
            ["stock-counter", "invoice-checker", "audit-fanin"],
            r"audit .* warehouse",
            "Audit the north warehouse",
        ),
        (
            "museum-catalog",
            "gallery-reconciliation",
            ["east-curator", "west-curator", "catalog-fanin"],
            r"reconcile .* gallery",
            "Reconcile the renamed gallery",
        ),
    ]
    native_session_id = f"chatds-{'9' * 32}"
    for skill_name, route_id, workers, pattern, user_text in cases:
        manifest = _workflow_manifest(
            skill_name=skill_name,
            route_id=route_id,
            worker_ids=workers,
            pattern=pattern,
        )
        contract = compile_turn_workflow_contract(
            manifest=manifest,
            user_turn_text=user_text,
            bound_skill_name=skill_name,
        )
        projection = compile_deepseek_workflow_projection(
            manifest=manifest,
            workflow_contract=contract,
            user_turn_text=user_text,
            native_session_id=native_session_id,
        )
        assert projection is not None
        assert projection["skill_name"] == skill_name
        assert projection["route_id"] == route_id
        assert [phase["phase_id"] for phase in projection["phases"]] == [
            "phase-0", "phase-1"
        ]
        assert [
            worker["worker_id"]
            for phase in projection["phases"]
            for worker in phase["workers"]
        ] == workers
        assert all(
            worker["source_path"].startswith("orchestration/workers/")
            for phase in projection["phases"]
            for worker in phase["workers"]
        )


def test_deepseek_workflow_receipt_rejects_failed_barrier_and_accepts_retry():
    manifest = _workflow_manifest(
        skill_name="warehouse-audit",
        route_id="full-ledger-audit",
        worker_ids=["stock-counter", "invoice-checker", "audit-fanin"],
        pattern=r"audit .* warehouse",
    )
    native_session_id = f"chatds-{'8' * 32}"
    contract = compile_turn_workflow_contract(
        manifest=manifest,
        user_turn_text="Audit the south warehouse",
        bound_skill_name="warehouse-audit",
    )
    projection = compile_deepseek_workflow_projection(
        manifest=manifest,
        workflow_contract=contract,
        user_turn_text="Audit the south warehouse",
        native_session_id=native_session_id,
    )
    assert projection is not None
    run_id = "native-run-1"
    run_name = projection["run_name"]
    failed = [
        _workflow_event(1, native_session_id, "tool-workflow/run-start", {
            "runId": run_id, "name": run_name,
        }),
        _workflow_event(2, native_session_id, "tool-workflow/agent-start", {
            "runId": run_id, "seq": 1, "label": "stock-counter",
            "phase": "phase-0", "childId": "child-a",
        }),
        _workflow_event(3, native_session_id, "tool-workflow/agent-start", {
            "runId": run_id, "seq": 2, "label": "invoice-checker",
            "phase": "phase-0", "childId": "child-b",
        }),
        _workflow_event(4, native_session_id, "tool-workflow/agent-end", {
            "runId": run_id, "seq": 1, "outcome": "completed",
        }),
        _workflow_event(5, native_session_id, "tool-workflow/agent-end", {
            "runId": run_id, "seq": 2, "outcome": "failed",
        }),
        # Failure injection: a later phase started before phase 0 passed.
        _workflow_event(6, native_session_id, "tool-workflow/agent-start", {
            "runId": run_id, "seq": 3, "label": "audit-fanin",
            "phase": "phase-1", "childId": "child-c",
        }),
        _workflow_event(7, native_session_id, "tool-workflow/agent-end", {
            "runId": run_id, "seq": 3, "outcome": "completed",
        }),
        _workflow_event(8, native_session_id, "tool-workflow/run-end", {
            "runId": run_id, "stopReason": "completed",
        }),
    ]
    receipt = build_deepseek_workflow_receipt(projection, failed)
    assert receipt["status"] == "failed"
    assert "workflow_phase_order_violation" in {
        finding["code"] for finding in receipt["findings"]
    }

    corrected = failed[:5] + [
        _workflow_event(6, native_session_id, "tool-workflow/agent-start", {
            "runId": run_id, "seq": 3, "label": "invoice-checker",
            "phase": "phase-0", "childId": "child-b-retry",
        }),
        _workflow_event(7, native_session_id, "tool-workflow/agent-end", {
            "runId": run_id, "seq": 3, "outcome": "completed",
        }),
        _workflow_event(8, native_session_id, "tool-workflow/agent-start", {
            "runId": run_id, "seq": 4, "label": "audit-fanin",
            "phase": "phase-1", "childId": "child-c",
        }),
        _workflow_event(9, native_session_id, "tool-workflow/agent-end", {
            "runId": run_id, "seq": 4, "outcome": "completed",
        }),
        _workflow_event(10, native_session_id, "tool-workflow/run-end", {
            "runId": run_id, "stopReason": "completed",
        }),
    ]
    passed = build_deepseek_workflow_receipt(projection, corrected)
    assert passed["status"] == "passed"
    assert passed["finding_count"] == 0
