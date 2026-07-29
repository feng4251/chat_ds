"""Repeatable real-socket acceptance runner for the unified session sandbox.

This is deployment-test orchestration, not Harness routing logic. It uses the
same authenticated client and exact Skill snapshots as production.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from typing import Any
from urllib.parse import urlsplit


HARNESS_ROOT = Path(os.environ.get("CHATDS_ACCEPTANCE_HARNESS_ROOT", "/test/harness"))
sys.path.insert(0, str(HARNESS_ROOT))
# Import the one low-level client without executing Harness's application-level
# tools package initializer (which intentionally loads unrelated services).
tools_package = types.ModuleType("tools")
tools_package.__path__ = [str(HARNESS_ROOT / "tools")]
sys.modules["tools"] = tools_package

from tools import isolated_skill_executor as client  # noqa: E402


def _workspace(parent: Path, name: str) -> Path:
    destination = parent / name
    destination.mkdir(mode=0o700)
    return destination


async def _read_until(
    lease: client.IsolatedProcessLease,
    *,
    predicate: Any,
    timeout: float = 120,
) -> tuple[bytes, bytes, dict[str, Any]]:
    stdout_offset = 0
    stderr_offset = 0
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await client.read_isolated_process_output(
            lease,
            stdout_offset=stdout_offset,
            stderr_offset=stderr_offset,
            wait_ms=1_000,
        )
        stdout.extend(latest["stdout_bytes"])
        stderr.extend(latest["stderr_bytes"])
        stdout_offset = latest["stdout_next_offset"]
        stderr_offset = latest["stderr_next_offset"]
        if predicate(bytes(stdout), bytes(stderr), latest):
            return bytes(stdout), bytes(stderr), latest
    raise AssertionError(
        f"process acceptance timeout; stdout={stdout[-2000:]!r}, "
        f"stderr={stderr[-2000:]!r}, state={latest.get('state')!r}"
    )


async def _run_cli(
    scope: client.ProcessOwnerScope,
    *,
    skill_root: Path,
    workspace: Path,
    entrypoint: str,
    expected: bytes,
    socket_path: str,
    egress_rules: tuple[dict[str, object], ...] = (),
    private_origins: tuple[str, ...] = (),
    args: list[str] | None = None,
) -> dict[str, Any]:
    lease, opened = await client.open_isolated_process_lease(
        owner_scope=scope,
        skill_root=skill_root,
        workspace=workspace,
        entrypoint=entrypoint,
        args=args,
        socket_path=socket_path,
        idle_ttl_seconds=120,
        max_runtime_seconds=300,
        egress_rules=egress_rules,
        private_origins=private_origins,
    )
    assert opened["runtime_profile"] == "session-sandbox-v1", opened
    await client.start_isolated_process_lease(lease)
    stdin_closed = await client.close_isolated_process_stdin(lease)
    assert stdin_closed["stdin_closed"] is True
    stdout, stderr, latest = await _read_until(
        lease,
        predicate=lambda out, _err, response: (
            response.get("state") == "exited"
            and response.get("stdout_eof") is True
        ),
    )
    assert latest["returncode"] == 0, (
        entrypoint,
        latest["returncode"],
        stdout,
        stderr,
    )
    assert expected in stdout, stdout
    closed = await client.close_isolated_process_lease(lease)
    assert closed["state"] == "closed"
    return {
        "entrypoint": entrypoint,
        "stdout": stdout.decode("utf-8", errors="replace").strip(),
        "stderr": stderr.decode("utf-8", errors="replace").strip(),
        "runtime_profile": opened["runtime_profile"],
    }


async def _abandon_running_lease(
    *,
    smoke_root: Path,
    socket_path: str,
) -> dict[str, Any]:
    scope = client.create_process_owner_scope(
        user_id="browser-abandon-acceptance",
        session_id="browser-abandon-acceptance",
        root_run_id=f"browser-abandon-acceptance-{os.getpid()}",
    )
    with tempfile.TemporaryDirectory(prefix="chatds-browser-abandon-") as text:
        lease, opened = await client.open_isolated_process_lease(
            owner_scope=scope,
            skill_root=smoke_root,
            workspace=_workspace(Path(text), "workspace"),
            entrypoint="long_running.py",
            socket_path=socket_path,
            idle_ttl_seconds=120,
            max_runtime_seconds=300,
        )
        await client.start_isolated_process_lease(lease)
        await client.close_isolated_process_stdin(lease)
        stdout, stderr, latest = await _read_until(
            lease,
            predicate=lambda out, _err, response: (
                b"long-running-ready" in out
                and response.get("state") == "running"
            ),
            timeout=30,
        )
        assert not stderr, stderr
        assert b"long-running-ready" in stdout
        # Deliberately do not close the lease. The caller restarts the
        # controller container and proves startup fixed-UID/tree cleanup.
        return {
            "runtime_profile": opened["runtime_profile"],
            "state_before_client_exit": latest["state"],
        }


def _result_events(output: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


async def _call_and_read(
    lease: client.IsolatedProcessLease,
    *,
    method_name: str,
    method_args: list[Any] | None = None,
    method_kwargs: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int, int]:
    ready, ready_error, ready_receipt = await _read_until(
        lease,
        predicate=lambda out, _err, _response: any(
            event.get("event") == "ready" for event in _result_events(out)
        ),
    )
    assert not ready_error, ready_error
    stdout_offset = ready_receipt["stdout_next_offset"]
    stderr_offset = ready_receipt["stderr_next_offset"]
    queued = await client.call_isolated_process_instance(
        lease,
        method_name=method_name,
        method_args=method_args,
        method_kwargs=method_kwargs,
    )
    call_id = queued["call_id"]
    result_bytes, error_bytes, receipt = await _read_until(
        lease,
        predicate=lambda out, _err, _response: any(
            event.get("event") == "call_result"
            and event.get("call_id") == call_id
            for event in _result_events(out)
        ),
    )
    # _read_until begins at zero. Slice by the previous offsets so this
    # assertion explicitly exercises the protocol's monotonic stream offsets.
    new_results = result_bytes[stdout_offset:]
    new_errors = error_bytes[stderr_offset:]
    assert not new_errors, new_errors
    matching = [
        event
        for event in _result_events(new_results)
        if event.get("event") == "call_result"
        and event.get("call_id") == call_id
    ]
    assert (
        len(matching) == 1
        and matching[0].get("status") == "success"
    ), matching
    return matching[0], receipt["stdout_next_offset"], receipt["stderr_next_offset"]


async def _run_persistent_probe(
    scope: client.ProcessOwnerScope,
    *,
    skill_root: Path,
    workspace: Path,
    socket_path: str,
    invocation: str,
    engine: str,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "owner_scope": scope,
        "skill_root": skill_root,
        "workspace": workspace,
        "entrypoint": "persistent_browser.py",
        "socket_path": socket_path,
        "constructor_args": [engine],
        "idle_ttl_seconds": 120,
        "max_runtime_seconds": 300,
    }
    if invocation == "class":
        parameters["class_name"] = "BrowserProbe"
    else:
        parameters["factory_name"] = "open_browser_probe"
    lease, opened = await client.open_isolated_process_lease(**parameters)
    await client.start_isolated_process_lease(lease)
    result, _, _ = await _call_and_read(
        lease,
        method_name="title",
        method_args=[f"{invocation}-{engine}-ok"],
    )
    assert result["result"] == f"{invocation}-{engine}-ok", result
    closed = await client.close_isolated_process_lease(lease)
    assert closed["state"] == "closed"
    return {
        "invocation": invocation,
        "engine": engine,
        "runtime_profile": opened["runtime_profile"],
    }


def _canonical_http_origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AssertionError(f"acceptance URL is not HTTP(S): {url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AssertionError(
            f"acceptance URL has an invalid port: {url!r}"
        ) from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}"


def _retrieval_rule(origin: str) -> dict[str, object]:
    """Return the exact root retrieval rule used by deployment acceptance."""

    canonical = _canonical_http_origin(origin)
    if canonical != origin:
        raise AssertionError(
            f"acceptance origin is not canonical: {origin!r}"
        )
    return {
        "methods": ["GET", "HEAD"],
        "url_prefix": canonical + "/",
    }


async def _run_exact_visual_skill(
    scope: client.ProcessOwnerScope,
    *,
    skill_root: Path,
    workspace: Path,
    socket_path: str,
    public_url: str | None = None,
) -> dict[str, Any]:
    public_origin = (
        _canonical_http_origin(public_url)
        if public_url
        else None
    )
    egress_rules = (
        (_retrieval_rule(public_origin),)
        if public_origin is not None
        else ()
    )
    lease, opened = await client.open_isolated_process_lease(
        owner_scope=scope,
        skill_root=skill_root,
        workspace=workspace,
        entrypoint="scripts/browser_operator.py",
        class_name="ChromeVisualSession",
        constructor_args=["output_result/visual-smoke"],
        socket_path=socket_path,
        idle_ttl_seconds=120,
        max_runtime_seconds=300,
        egress_rules=egress_rules,
        private_origins=(),
    )
    await client.start_isolated_process_lease(lease)
    observed, stdout_offset, stderr_offset = await _call_and_read(
        lease,
        method_name="observe",
        method_args=["acceptance"],
        method_kwargs={"full_page": True},
    )
    assert observed["result"]["artifacts"]["viewport"], observed
    public_observation: dict[str, Any] | None = None
    if public_url:
        opened_public, stdout_offset, stderr_offset = await _call_and_read(
            lease,
            method_name="open",
            method_args=[public_url],
            method_kwargs={"observe": True},
        )
        public_observation = opened_public["result"]
        assert public_observation["url"].startswith("https://"), public_observation
        assert public_observation["artifacts"]["viewport"], public_observation
    synced = await client.sync_isolated_process_artifacts(lease)
    assert synced["state"] == "running"
    root = workspace / "output_result/visual-smoke"
    viewport = next(root.glob("*-viewport.png"))
    html = next(root.glob("*.html"))
    dom = next(root.glob("*-dom.json"))
    network_events = json.loads(
        (root / "network-events.json").read_text(encoding="utf-8")
    )
    assert viewport.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "<html" in html.read_text(encoding="utf-8").lower()
    assert isinstance(json.loads(dom.read_text(encoding="utf-8")), dict)

    queued = await client.call_isolated_process_instance(
        lease,
        method_name="close",
    )
    call_id = queued["call_id"]
    closed_output = await client.read_isolated_process_output(
        lease,
        stdout_offset=stdout_offset,
        stderr_offset=stderr_offset,
        wait_ms=10_000,
    )
    assert any(
        event.get("event") == "call_result"
        and event.get("call_id") == call_id
        and event.get("status") == "success"
        for event in _result_events(closed_output["stdout_bytes"])
    ), closed_output["stdout_bytes"]
    closed = await client.close_isolated_process_lease(lease)
    assert closed["state"] == "closed"
    return {
        "runtime_profile": opened["runtime_profile"],
        "viewport_bytes": viewport.stat().st_size,
        "public_url_observed": bool(public_observation),
        "public_title": (
            str(public_observation.get("title", ""))[:200]
            if public_observation
            else ""
        ),
        "public_warnings": (
            public_observation.get("warnings", [])
            if public_observation
            else []
        ),
        "public_network_events": [
            {
                key: event.get(key)
                for key in (
                    "event",
                    "method",
                    "status",
                    "statusText",
                    "mimeType",
                    "protocol",
                )
                if key in event
            }
            for event in network_events[-8:]
            if isinstance(event, dict)
        ],
        "artifacts": sorted(
            str(path.relative_to(workspace)) for path in root.iterdir()
        ),
    }


async def _main(arguments: argparse.Namespace) -> dict[str, Any]:
    socket_path = arguments.socket
    smoke_root = Path(arguments.smoke_skill_root)
    if arguments.abandon_running_lease:
        return await _abandon_running_lease(
            smoke_root=smoke_root,
            socket_path=socket_path,
        )
    reaped = await client.reap_isolated_executor_leases(
        socket_path=socket_path
    )
    exact_root = (
        Path(arguments.exact_skill_root)
        if arguments.exact_skill_root
        else None
    )
    scope = client.create_process_owner_scope(
        user_id="browser-acceptance",
        session_id="browser-acceptance",
        root_run_id=f"browser-acceptance-{os.getpid()}",
    )
    with tempfile.TemporaryDirectory(prefix="chatds-browser-acceptance-") as text:
        parent = Path(text)
        results: dict[str, Any] = {
            "startup_reaped_leases": reaped["reaped_leases"],
            "cli": [],
        }
        cli_cases = (
            ("node_playwright.cjs", b"node-playwright-ok"),
            ("node_playwright.mjs", b"node-playwright-esm-ok"),
            ("python_browsers.py", b"python-playwright-selenium-ok"),
            ("ipc_denied.py", b"ipc-denied-ok"),
            ("network_identity_probe.py", b"network-identity-ok"),
            ("escape_descendant.py", b"escape-descendant-ok"),
        )
        if arguments.cli_only == "base_identity_probe.py":
            cli_cases = (("base_identity_probe.py", b"base-identity-ok"),)
        elif arguments.cli_only:
            cli_cases = tuple(
                case for case in cli_cases if case[0] == arguments.cli_only
            )
        for index, (entrypoint, expected) in enumerate(cli_cases):
            egress_rules: tuple[dict[str, object], ...] = ()
            private_origins: tuple[str, ...] = ()
            script_args: list[str] | None = None
            if entrypoint == "network_identity_probe.py":
                public_origin = "https://example.com:443"
                egress_rules = (_retrieval_rule(public_origin),)
                if arguments.private_origin:
                    private_origin = _canonical_http_origin(
                        arguments.private_origin
                    )
                    parsed_private = urlsplit(private_origin)
                    assert parsed_private.hostname is not None
                    assert parsed_private.port is not None
                    egress_rules = (
                        *egress_rules,
                        _retrieval_rule(private_origin),
                    )
                    private_origins = (private_origin,)
                    script_args = [
                        parsed_private.hostname,
                        str(parsed_private.port),
                    ]
            results["cli"].append(
                await _run_cli(
                    scope,
                    skill_root=smoke_root,
                    workspace=_workspace(parent, f"cli-{index}"),
                    entrypoint=entrypoint,
                    expected=expected,
                    socket_path=socket_path,
                    egress_rules=egress_rules,
                    private_origins=private_origins,
                    args=script_args,
                )
            )
        if arguments.cli_only:
            return results
        results["persistent_class"] = await _run_persistent_probe(
            scope,
            skill_root=smoke_root,
            workspace=_workspace(parent, "persistent-class"),
            socket_path=socket_path,
            invocation="class",
            engine="playwright",
        )
        results["persistent_factory"] = await _run_persistent_probe(
            scope,
            skill_root=smoke_root,
            workspace=_workspace(parent, "persistent-factory"),
            socket_path=socket_path,
            invocation="factory",
            engine="selenium",
        )
        large_workspace = _workspace(parent, "large")
        results["large_artifact_process"] = await _run_cli(
            scope,
            skill_root=smoke_root,
            workspace=large_workspace,
            entrypoint="large_visual_artifact.py",
            expected=b"large-visual-artifact-ok:",
            socket_path=socket_path,
        )
        large = large_workspace / "output_result/visual-smoke-over-8mib.png"
        assert 8 * 1024 * 1024 < large.stat().st_size < 24 * 1024 * 1024
        results["large_artifact_bytes"] = large.stat().st_size
        if exact_root is not None:
            results["exact_visual_skill"] = await _run_exact_visual_skill(
                scope,
                skill_root=exact_root,
                workspace=_workspace(parent, "exact-visual"),
                socket_path=socket_path,
                public_url=arguments.exact_public_url,
            )
        return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--socket",
        default="/run/chat-ds-executor/executor.sock",
    )
    parser.add_argument("--smoke-skill-root", required=True)
    parser.add_argument("--exact-skill-root")
    parser.add_argument("--exact-public-url")
    parser.add_argument(
        "--private-origin",
        help=(
            "Optional deployment-allowlisted private HTTPS origin used by "
            "the network identity probe."
        ),
    )
    parser.add_argument("--abandon-running-lease", action="store_true")
    parser.add_argument(
        "--cli-only",
        choices=(
            "node_playwright.cjs",
            "node_playwright.mjs",
            "python_browsers.py",
            "ipc_denied.py",
            "network_identity_probe.py",
            "escape_descendant.py",
            "base_identity_probe.py",
        ),
    )
    arguments = parser.parse_args()
    result = asyncio.run(_main(arguments))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
