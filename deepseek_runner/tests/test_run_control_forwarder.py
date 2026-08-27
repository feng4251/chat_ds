import os
import stat
import time
from pathlib import Path

from deepseek_runner.runner_entrypoint import RunControlForwarder
from native_security.run_control import (
    build_run_control,
    enqueue_run_control,
    read_run_control_receipt,
    receipt_path,
    request_path,
    write_run_control_receipt,
)


class _Ledger:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def _wait(path: Path) -> None:
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


def test_dsh_forwarder_copies_only_native_delivery_receipts(tmp_path: Path):
    controller = tmp_path / "controller"
    worker = tmp_path / "worker"
    ledger = _Ledger()
    forwarder = RunControlForwarder(
        controller,
        worker,
        ledger,
        worker_uid=os.getuid(),
        worker_gid=os.getgid(),
    )
    interrupt = build_run_control(
        control_id="a" * 32,
        seq=1,
        action="interrupt",
        text=None,
    )
    followup = build_run_control(
        control_id="b" * 32,
        seq=2,
        action="followup",
        text="Continue with the renamed planetarium log.",
    )
    enqueue_run_control(controller, interrupt)
    forwarder.start()
    try:
        _wait(request_path(worker, interrupt["control_id"]))
        worker_request = os.lstat(
            request_path(worker, interrupt["control_id"])
        )
        assert stat.S_IMODE(worker_request.st_mode) == 0o640
        assert worker_request.st_nlink == 1
        assert stat.S_IMODE(os.lstat(worker / "requests").st_mode) == 0o750
        write_run_control_receipt(worker, interrupt, status="delivered")
        _wait(receipt_path(controller, interrupt["control_id"]))
        assert forwarder.interruption_pending is True

        enqueue_run_control(controller, followup)
        _wait(request_path(worker, followup["control_id"]))
        write_run_control_receipt(worker, followup, status="delivered")
        _wait(receipt_path(controller, followup["control_id"]))
    finally:
        forwarder.stop()
    assert forwarder.interruption_pending is False
    assert read_run_control_receipt(
        receipt_path(controller, followup["control_id"]),
        request=followup,
    )["status"] == "delivered"
    assert [event["action"] for event in ledger.events] == [
        "interrupt", "followup",
    ]
