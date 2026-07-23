"""Application-level acceptance for Harness persistent Skill execution.

Unlike ``process_lease_acceptance.py``, this runner enters through the public
``run_skill_process`` adapter.  It proves that one immutable Skill snapshot is
authorized, classified, routed to the correct executor socket, and operated
through the same opaque process capability exposed to a model.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from typing import Any
import uuid


HARNESS_ROOT = Path(
    os.environ.get("CHATDS_ACCEPTANCE_HARNESS_ROOT", "/test/harness")
)
sys.path.insert(0, str(HARNESS_ROOT))
# Loading ``tools.__init__`` would initialize every unrelated Harness service.
# Expose only the real tools package path, then import the production adapter.
tools_package = types.ModuleType("tools")
tools_package.__path__ = [str(HARNESS_ROOT / "tools")]
sys.modules["tools"] = tools_package

from tools.context import ToolContext  # noqa: E402
from tools import skill_process  # noqa: E402


SKILL_NAME = "browser-runtime-smoke"


def _receipt(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if value.get("status") != "success":
        raise AssertionError(value)
    return value


def _operation_context(context: ToolContext) -> ToolContext:
    """Production assigns a fresh native tool-call id to every operation."""

    return replace(context, tool_operation_id=str(uuid.uuid4()))


def _script_digest(skill_root: Path, resource: str) -> str:
    return hashlib.sha256((skill_root / resource).read_bytes()).hexdigest()


def _build_context(skill_root: Path) -> tuple[ToolContext, dict[str, str]]:
    resources = (
        "base_identity_probe.py",
        "bash_direct_helper.sh",
        "node_playwright.cjs",
        "persistent_browser.py",
    )
    snapshot = skill_process.snapshot_skill_package(skill_root)
    root_digest = snapshot.file_sha256("SKILL.md")
    digests = {
        resource: snapshot.file_sha256(resource)
        for resource in resources
    }
    context = ToolContext(
        user_id="harness-process-acceptance",
        session_id=f"harness-process-acceptance-{os.getpid()}",
        run_id=f"harness-process-acceptance-{os.getpid()}",
        root_run_id=f"harness-process-acceptance-{os.getpid()}",
        enabled_user_skills=(SKILL_NAME,),
        skill_execution_resource_boundary=True,
        allowed_skill_scripts=tuple(
            (SKILL_NAME, resource, digests[resource])
            for resource in resources
        ),
        process_only_skill_scripts=tuple(
            (SKILL_NAME, resource, digests[resource])
            for resource in (
                "node_playwright.cjs",
                "persistent_browser.py",
            )
        ),
        allowed_skill_script_authorities=tuple(
            (
                SKILL_NAME,
                root_digest,
                "SKILL.md",
                root_digest,
                resource,
                digests[resource],
            )
            for resource in resources
        ),
        allowed_skill_package_digests=((SKILL_NAME, snapshot.sha256),),
        tool_operation_id=str(uuid.uuid4()),
    )
    return context, {
        "package_sha256": snapshot.sha256,
        **{f"{resource}_sha256": digest for resource, digest in digests.items()},
    }


def _resolve_factory(skill_root: Path):
    root = skill_root.resolve(strict=True)

    def _resolve(
        script_path: str,
        user_id: str,
        session_id: str,
        enabled_user_skills: list[str],
    ) -> tuple[Path, Path, str]:
        del user_id, session_id
        prefix = f"skills/{SKILL_NAME}/"
        if SKILL_NAME not in enabled_user_skills or not script_path.startswith(
            prefix
        ):
            raise AssertionError(f"unexpected Skill path {script_path!r}")
        candidate = (root / script_path.removeprefix(prefix)).resolve(strict=True)
        candidate.relative_to(root)
        return candidate, root, SKILL_NAME

    return _resolve


def _assert_snapshot_record(
    process_id: str,
    *,
    expected_profile: str,
    expected_package_sha256: str,
    expected_script_sha256: str,
) -> None:
    record = skill_process._MANAGER._records[process_id]
    assert record.runtime_profile == expected_profile, record.runtime_profile
    assert record.authority.package_sha256 == expected_package_sha256
    assert record.authority.script_sha256 == expected_script_sha256
    assert record.lease.skill_sha256 == expected_package_sha256
    assert record.lease.script_sha256 == expected_script_sha256


async def _close(
    process_id: str | None,
    context: ToolContext,
) -> dict[str, Any] | None:
    if process_id is None:
        return None
    raw = await skill_process.run_skill_process(
        operation="close",
        process_id=process_id,
        context=_operation_context(context),
    )
    return _receipt(raw)


async def _read_until(
    process_id: str,
    context: ToolContext,
    *,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
    predicate: Any,
    timeout: float = 180,
) -> tuple[str, str, dict[str, Any]]:
    stdout = ""
    stderr = ""
    latest: dict[str, Any] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = _receipt(
            await skill_process.run_skill_process(
                operation="read",
                process_id=process_id,
                stdout_offset=stdout_offset,
                stderr_offset=stderr_offset,
                wait_ms=1_000,
                context=_operation_context(context),
            )
        )
        stdout += latest.get("stdout_text", "")
        stderr += latest.get("stderr_text", "")
        stdout_offset = latest["stdout_next_offset"]
        stderr_offset = latest["stderr_next_offset"]
        if predicate(stdout, stderr, latest):
            return stdout, stderr, latest
        if (
            latest.get("state") == "exited"
            and latest.get("stdout_eof") is True
            and latest.get("stderr_eof") is True
        ):
            raise AssertionError(
                "Harness process exited before satisfying acceptance; "
                f"returncode={latest.get('returncode')!r}, "
                f"stdout={stdout[-2_000:]!r}, stderr={stderr[-2_000:]!r}"
            )
    raise AssertionError(
        "Harness process acceptance timeout; "
        f"stdout={stdout[-2_000:]!r}, stderr={stderr[-2_000:]!r}, "
        f"state={latest.get('state')!r}"
    )


async def _run_cli(
    context: ToolContext,
    digests: dict[str, str],
    *,
    resource: str,
    expected_profile: str,
    expected_stdout: str,
) -> dict[str, Any]:
    process_id: str | None = None
    try:
        started = _receipt(
            await skill_process.run_skill_process(
                operation="start",
                script_path=f"skills/{SKILL_NAME}/{resource}",
                idle_ttl_seconds=120,
                max_runtime_seconds=300,
                context=_operation_context(context),
            )
        )
        process_id = started["process_id"]
        assert started["runtime_profile"] == expected_profile, started
        _assert_snapshot_record(
            process_id,
            expected_profile=expected_profile,
            expected_package_sha256=digests["package_sha256"],
            expected_script_sha256=digests[f"{resource}_sha256"],
        )
        stdin_closed = _receipt(
            await skill_process.run_skill_process(
                operation="stdin_close",
                process_id=process_id,
                context=_operation_context(context),
            )
        )
        assert stdin_closed["stdin_closed"] is True, stdin_closed
        stdout, stderr, final = await _read_until(
            process_id,
            context,
            predicate=lambda out, _err, receipt: (
                expected_stdout in out
                and receipt.get("state") == "exited"
                and receipt.get("stdout_eof") is True
            ),
        )
        assert final["returncode"] == 0, (stdout, stderr, final)
        assert expected_stdout in stdout, stdout
        return {
            "resource": resource,
            "runtime_profile": started["runtime_profile"],
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "returncode": final["returncode"],
            "snapshot_identity": "matched",
        }
    finally:
        await _close(process_id, context)


def _jsonl_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


async def _run_persistent_browser(
    context: ToolContext,
    digests: dict[str, str],
) -> dict[str, Any]:
    resource = "persistent_browser.py"
    process_id: str | None = None
    try:
        started = _receipt(
            await skill_process.run_skill_process(
                operation="start",
                script_path=f"skills/{SKILL_NAME}/{resource}",
                class_name="BrowserProbe",
                constructor_args=["playwright"],
                idle_ttl_seconds=120,
                max_runtime_seconds=300,
                context=_operation_context(context),
            )
        )
        process_id = started["process_id"]
        assert started["runtime_profile"] == "browser-automation-v1", started
        assert started["invocation_mode"] == "instance", started
        _assert_snapshot_record(
            process_id,
            expected_profile="browser-automation-v1",
            expected_package_sha256=digests["package_sha256"],
            expected_script_sha256=digests[f"{resource}_sha256"],
        )
        ready_stdout, ready_stderr, ready = await _read_until(
            process_id,
            context,
            predicate=lambda out, _err, _receipt_value: any(
                event.get("event") == "ready"
                for event in _jsonl_events(out)
            ),
        )
        assert not ready_stderr, ready_stderr
        called = _receipt(
            await skill_process.run_skill_process(
                operation="call",
                process_id=process_id,
                method_name="title",
                method_args=["harness-persistent-browser-ok"],
                context=_operation_context(context),
            )
        )
        call_id = called["call_id"]
        result_stdout, result_stderr, _result_receipt = await _read_until(
            process_id,
            context,
            stdout_offset=ready["stdout_next_offset"],
            stderr_offset=ready["stderr_next_offset"],
            predicate=lambda out, _err, _receipt_value: any(
                event.get("event") == "call_result"
                and event.get("call_id") == call_id
                for event in _jsonl_events(out)
            ),
        )
        assert not result_stderr, result_stderr
        result = next(
            event
            for event in _jsonl_events(result_stdout)
            if (
                event.get("event") == "call_result"
                and event.get("call_id") == call_id
            )
        )
        assert result["status"] == "success", result
        assert result["result"] == "harness-persistent-browser-ok", result
        return {
            "resource": resource,
            "runtime_profile": started["runtime_profile"],
            "invocation_mode": started["invocation_mode"],
            "ready_event": any(
                event.get("event") == "ready"
                for event in _jsonl_events(ready_stdout)
            ),
            "call_result": result["result"],
            "snapshot_identity": "matched",
        }
    finally:
        await _close(process_id, context)


async def _main(arguments: argparse.Namespace) -> dict[str, Any]:
    skill_root = Path(arguments.skill_root).resolve(strict=True)
    context, digests = _build_context(skill_root)
    manager = skill_process.SkillProcessManager()
    old_manager = skill_process._MANAGER
    old_resolver = skill_process._resolve_session_skill_script
    old_sandbox_dir = skill_process.sandbox_dir
    with tempfile.TemporaryDirectory(
        prefix="chatds-harness-process-acceptance-"
    ) as text:
        workspace = Path(text) / "workspace"
        workspace.mkdir(mode=0o700)
        skill_process._MANAGER = manager
        skill_process._resolve_session_skill_script = _resolve_factory(skill_root)
        skill_process.sandbox_dir = lambda *_args, **_kwargs: workspace
        try:
            result = {
                "base_cli": await _run_cli(
                    context,
                    digests,
                    resource="base_identity_probe.py",
                    expected_profile="base-v1",
                    expected_stdout="base-identity-ok",
                ),
                "bash_direct_helper": await _run_cli(
                    context,
                    digests,
                    resource="bash_direct_helper.sh",
                    expected_profile="base-v1",
                    expected_stdout="bash-direct-helper-ok",
                ),
                "browser_cli": await _run_cli(
                    context,
                    digests,
                    resource="node_playwright.cjs",
                    expected_profile="browser-automation-v1",
                    expected_stdout="node-playwright-ok",
                ),
                "persistent_browser": await _run_persistent_browser(
                    context,
                    digests,
                ),
                "authorized_package_sha256": digests["package_sha256"],
            }
            cleaned = await manager.close_all()
            assert cleaned["success"] is True, cleaned
            assert cleaned["matched"] == 0, cleaned
            result["final_manager_cleanup"] = cleaned
            return result
        finally:
            await manager.close_all()
            skill_process._MANAGER = old_manager
            skill_process._resolve_session_skill_script = old_resolver
            skill_process.sandbox_dir = old_sandbox_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skill-root",
        default="/test/browser-runtime-smoke",
    )
    print(json.dumps(asyncio.run(_main(parser.parse_args())), indent=2))
