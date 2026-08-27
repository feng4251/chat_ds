import os
import stat
from pathlib import Path

import pytest

from native_security.run_control import (
    RunControlError,
    build_run_control,
    enqueue_run_control,
    list_run_controls,
    next_run_control_seq,
    read_run_control_receipt,
    receipt_path,
    write_run_control_receipt,
)


def test_generic_controls_are_ordered_idempotent_and_receipted(tmp_path: Path):
    root = tmp_path / "controls"
    first = build_run_control(
        control_id="1" * 32,
        seq=1,
        action="followup",
        text="Reconcile the renamed warehouse manifest.",
    )
    second = build_run_control(
        control_id="2" * 32,
        seq=2,
        action="steer",
        text="Prioritize the inventory discrepancy.",
    )
    assert enqueue_run_control(root, second) is False
    assert enqueue_run_control(root, first) is False
    assert enqueue_run_control(root, first) is True
    assert [item["seq"] for item in list_run_controls(root)] == [1, 2]
    assert next_run_control_seq(root) == 3

    receipt = write_run_control_receipt(root, first, status="delivered")
    assert receipt["status"] == "delivered"
    assert write_run_control_receipt(root, first, status="delivered") == receipt
    assert read_run_control_receipt(
        receipt_path(root, first["control_id"]), request=first
    ) == receipt


def test_interrupt_has_no_text_and_messages_require_text():
    build_run_control(
        control_id="a" * 32,
        seq=1,
        action="interrupt",
        text=None,
    )
    with pytest.raises(RunControlError, match="run_control_text_invalid"):
        build_run_control(
            control_id="b" * 32,
            seq=2,
            action="interrupt",
            text="fixture-specific escape",
        )
    with pytest.raises(RunControlError, match="run_control_text_invalid"):
        build_run_control(
            control_id="c" * 32,
            seq=3,
            action="followup",
            text=" ",
        )


def test_control_id_cannot_be_reused_for_different_content(tmp_path: Path):
    root = tmp_path / "controls"
    original = build_run_control(
        control_id="d" * 32,
        seq=1,
        action="followup",
        text="Inspect the gallery ledger.",
    )
    conflict = build_run_control(
        control_id="d" * 32,
        seq=1,
        action="followup",
        text="Change the gallery ledger.",
    )
    enqueue_run_control(root, original)
    with pytest.raises(RunControlError, match="run_control_id_conflict"):
        enqueue_run_control(root, conflict)


def test_atomic_request_can_be_published_read_only_to_a_worker_group(tmp_path: Path):
    root = tmp_path / "controls"
    request = build_run_control(
        control_id="e" * 32,
        seq=1,
        action="followup",
        text="Inspect the renamed transit manifest.",
    )
    enqueue_run_control(
        root,
        request,
        mode=0o640,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    info = os.lstat(root / "requests" / f"{request['control_id']}.json")
    assert stat.S_IMODE(info.st_mode) == 0o640
    assert info.st_uid == os.getuid()
    assert info.st_gid == os.getgid()
    assert info.st_nlink == 1
