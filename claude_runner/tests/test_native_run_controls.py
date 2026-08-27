import json
import tempfile
from pathlib import Path

from claude_runner.runner_entrypoint import (
    EventLedger,
    _native_run_control_input,
)
from native_security.run_control import build_run_control


def test_claude_controls_lower_to_the_public_stream_json_protocol():
    interrupt = build_run_control(
        control_id="1" * 32,
        seq=1,
        action="interrupt",
        text=None,
    )
    followup = build_run_control(
        control_id="2" * 32,
        seq=2,
        action="followup",
        text="Reconcile the renamed railway manifest.",
    )
    steer = build_run_control(
        control_id="3" * 32,
        seq=3,
        action="steer",
        text="Prioritize the latest carriage count.",
    )
    assert json.loads(_native_run_control_input(interrupt)) == {
        "type": "control_request",
        "request_id": "11111111-1111-1111-1111-111111111111",
        "request": {"subtype": "interrupt"},
    }
    assert json.loads(_native_run_control_input(followup))["priority"] == "later"
    assert json.loads(_native_run_control_input(steer))["priority"] == "now"


def test_authorized_followup_expands_result_frontier_without_accepting_extras():
    with tempfile.TemporaryDirectory() as temporary:
        ledger = EventLedger(Path(temporary) / "events.jsonl")
        ledger.record_native_control_delivery("followup")
        assert ledger.expected_native_result_count == 2
        result = b'{"type":"result","subtype":"success","is_error":false}'
        ledger.append_line(result, channel="stdout")
        ledger.append_line(result, channel="stdout")
        assert ledger.native_result_count == 2
        assert ledger.native_result_succeeded is True
        ledger.append_line(result, channel="stdout")
        assert ledger.native_result_count == 3
        assert ledger.native_result_succeeded is False
        ledger.close()


def test_interrupt_preserves_session_and_a_later_message_continues_it():
    with tempfile.TemporaryDirectory() as temporary:
        ledger = EventLedger(Path(temporary) / "events.jsonl")
        ledger.record_native_control_delivery("interrupt")
        assert ledger.native_interruption_pending is True
        ledger.record_native_control_delivery("steer")
        assert ledger.native_interruption_pending is False
        assert ledger.expected_native_result_count == 2
        ledger.close()
