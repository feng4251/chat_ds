"""Run Chromium behind a private Unix-domain CDP transport.

The process intentionally has no HTTP listener.  Chromium binds its ephemeral
DevTools port to the sidecar loopback interface and this service forwards a
dedicated Unix socket to it.  Only containers mounting that socket volume can
control the browser; the sidecar's egress network never carries CDP traffic.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import stat
import sys
import time
from typing import Final
from urllib.parse import urlsplit


SOCKET_PATH: Final[Path] = Path(
    os.environ.get("BROWSER_CDP_SOCKET", "/run/chat-ds-browser/cdp.sock")
)
PROFILE_DIR: Final[Path] = Path("/tmp/chromium-profile")
ACTIVE_PORT_FILE: Final[Path] = PROFILE_DIR / "DevToolsActivePort"
STARTUP_TIMEOUT_SECONDS: Final[float] = 30.0
PROCESS_TERMINATION_TIMEOUT_SECONDS: Final[float] = 5.0
PROCESS_GROUP_DRAIN_TIMEOUT_SECONDS: Final[float] = 1.0
LOG_DRAIN_TIMEOUT_SECONDS: Final[float] = 2.0
_URL_IN_LOG_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?:https?|wss?|ftp)://[^\s<>\]\[\"']+"
)
_TLS_SPKI_SHA256_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9+/]{43}=?"
)


def _read_active_port() -> tuple[int, str] | None:
    """Read and strictly validate Chromium's private discovery file."""

    try:
        lines = ACTIVE_PORT_FILE.read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        websocket_path = lines[1].strip()
    except (OSError, ValueError, IndexError):
        return None
    if not (1 <= port <= 65535):
        return None
    if not websocket_path.startswith("/devtools/browser/"):
        return None
    return port, websocket_path


async def _wait_for_active_port(process: asyncio.subprocess.Process) -> tuple[int, str]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise RuntimeError(
                f"Chromium exited before DevTools became ready (status {process.returncode})"
            )
        active = _read_active_port()
        if active is not None:
            return active
        await asyncio.sleep(0.05)
    raise RuntimeError("Timed out waiting for Chromium DevToolsActivePort")


async def _copy_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            data = await reader.read(64 * 1024)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


async def _proxy_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    chrome_port: int,
) -> None:
    try:
        chrome_reader, chrome_writer = await asyncio.open_connection(
            "127.0.0.1", chrome_port
        )
    except (ConnectionError, OSError):
        client_writer.close()
        await client_writer.wait_closed()
        return

    upstream = asyncio.create_task(_copy_stream(client_reader, chrome_writer))
    downstream = asyncio.create_task(_copy_stream(chrome_reader, client_writer))
    _done, pending = await asyncio.wait(
        {upstream, downstream}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(upstream, downstream, return_exceptions=True)
    chrome_writer.close()
    client_writer.close()
    await asyncio.gather(
        chrome_writer.wait_closed(),
        client_writer.wait_closed(),
        return_exceptions=True,
    )


async def _drain_chromium_logs(process: asyncio.subprocess.Process) -> None:
    if process.stdout is None:
        return
    while True:
        line = await process.stdout.readline()
        if not line:
            return
        # Chromium emits useful crash/sandbox diagnostics here and may also
        # include a page URL in an error.  Retain the diagnostic while
        # removing URL-shaped values so browsing history never becomes an
        # ambient container log.
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            text = _URL_IN_LOG_RE.sub("<redacted-url>", text)
            print(f"chromium: {text}", file=sys.stderr, flush=True)


def _prepare_runtime_dirs() -> None:
    # Browser state is intentionally disposable.  Remove the whole profile on
    # every process start so a stale DevToolsActivePort or Singleton* marker
    # can never bind the new UDS relay to a dead Chromium instance.  lstat()
    # plus the fixed in-container path prevents following an unexpected
    # profile symlink during cleanup.
    try:
        profile_mode = PROFILE_DIR.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISDIR(profile_mode):
            shutil.rmtree(PROFILE_DIR)
        else:
            PROFILE_DIR.unlink()
    PROFILE_DIR.mkdir(parents=True, mode=0o700)
    PROFILE_DIR.chmod(0o700)

    for directory in (
        Path(os.environ["HOME"]),
        Path(os.environ["XDG_CACHE_HOME"]),
        Path(os.environ["XDG_CONFIG_HOME"]),
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_directory = SOCKET_PATH.parent
    socket_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not stat.S_ISDIR(socket_directory.lstat().st_mode):
        raise RuntimeError("Browser CDP socket parent is not a directory")
    # The UDS is deliberately a privileged sidecar/Harness channel.  A
    # distinct untrusted subprocess UID must not be able to traverse the
    # shared volume and bypass the token-authenticated Harness relay.
    socket_directory.chmod(0o700)
    try:
        mode = SOCKET_PATH.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError("Refusing to replace non-socket CDP control path")
    SOCKET_PATH.unlink()


def _tls_spki_allowlist_switch() -> str | None:
    """Compile a deployment-owned, certificate-scoped Chromium exception.

    Private origins may use certificates issued by an internal CA that is not
    present in the immutable browser image.  A global
    ``--ignore-certificate-errors`` would silently weaken every destination.
    Chromium's SPKI allowlist keeps the exception bound to exact public-key
    hashes while the Harness independently enforces the per-turn origin grant.
    """

    configured = str(
        os.environ.get("BROWSER_TLS_SPKI_ALLOWLIST", "") or ""
    ).strip()
    if not configured:
        return None
    values = [
        item
        for item in re.split(r"[\s,]+", configured)
        if item
    ]
    if not values or any(
        _TLS_SPKI_SHA256_RE.fullmatch(item) is None
        for item in values
    ):
        raise RuntimeError(
            "BROWSER_TLS_SPKI_ALLOWLIST must contain only comma-separated "
            "base64 SHA-256 SPKI hashes"
        )
    deduplicated = list(dict.fromkeys(values))
    return (
        "--ignore-certificate-errors-spki-list="
        + ",".join(deduplicated)
    )


def _chromium_command() -> list[str]:
    executable = os.environ.get("CHROMIUM_EXECUTABLE", "/usr/bin/chromium")
    command = [
        executable,
        "--headless=new",
        # The sidecar runs non-root with no-new-privileges, so the setuid
        # helper is intentionally unavailable.  Chromium instead creates its
        # user-namespace sandbox. Compose relaxes the outer Docker seccomp
        # profile for that bootstrap while still dropping every Linux
        # capability and retaining Chromium's own renderer seccomp filters.
        "--disable-setuid-sandbox",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        # Browser pages must not acquire an egress path that bypasses the
        # Harness HTTP(S) request policy.  QUIC is the substrate used by
        # WebTransport, while this WebRTC policy prevents direct/non-proxied
        # UDP candidates.  The Harness also removes the corresponding page
        # APIs before site JavaScript runs as a second, independent layer.
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-sync",
        "--metrics-recording-only",
        "--password-store=basic",
    ]
    tls_spki_switch = _tls_spki_allowlist_switch()
    if tls_spki_switch is not None:
        command.append(tls_spki_switch)
    command.append("about:blank")
    return command


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    # Chromium descendants share the session/process group created in
    # ``serve``.  Signal that group even when the leader already exited: a
    # crashed leader can leave renderer/utility children holding stdout open.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    if process.returncode is None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                process.wait(),
                timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
            )

    async def wait_for_empty_group() -> None:
        while True:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                # The group still exists even if this process can no longer
                # signal it; retain the fail-closed timeout/KILL path.
                pass
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(
            wait_for_empty_group(),
            timeout=PROCESS_GROUP_DRAIN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        await process.wait()


async def _finish_log_task(log_task: asyncio.Task[None]) -> None:
    """Bound log-pipe draining so orphaned descriptors cannot hang PID 1."""

    try:
        await asyncio.wait_for(log_task, timeout=LOG_DRAIN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log_task.cancel()
        await asyncio.gather(log_task, return_exceptions=True)


async def serve() -> int:
    _prepare_runtime_dirs()
    process = await asyncio.create_subprocess_exec(
        *_chromium_command(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    log_task = asyncio.create_task(_drain_chromium_logs(process))
    server: asyncio.AbstractServer | None = None
    try:
        chrome_port, websocket_path = await _wait_for_active_port(process)

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await _proxy_connection(reader, writer, chrome_port)

        server = await asyncio.start_unix_server(handler, path=str(SOCKET_PATH))
        SOCKET_PATH.chmod(0o660)
        # The path is generated by Chromium, not a caller, and is logged only
        # to make startup/debug compatibility auditable.
        print(
            f"CDP Unix transport ready (DevToolsActivePort path {websocket_path})",
            flush=True,
        )

        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, shutdown.set)
        process_wait = asyncio.create_task(process.wait())
        shutdown_wait = asyncio.create_task(shutdown.wait())
        done, pending = await asyncio.wait(
            {process_wait, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if process_wait in done and process.returncode not in {None, 0}:
            return int(process.returncode or 1)
        return 0
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            SOCKET_PATH.unlink()
        await _terminate_process_group(process)
        await _finish_log_task(log_task)


def healthcheck() -> int:
    """Verify that the UDS reaches a real Chromium browser endpoint."""

    request = (
        b"GET /json/version HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Connection: close\r\n\r\n"
    )
    client: socket.socket | None = None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(str(SOCKET_PATH))
        client.sendall(request)
        response_buffer = bytearray()
        while len(response_buffer) < 128 * 1024:
            chunk = client.recv(16 * 1024)
            if not chunk:
                break
            response_buffer.extend(chunk)
            if _http_response_has_complete_body(response_buffer):
                # Chromium may keep the HTTP/1.1 connection alive even when
                # the request asks it to close.  A complete Content-Length
                # response is authoritative; waiting for EOF would turn a
                # healthy browser into a socket-timeout failure.
                break
    except OSError:
        return 1
    finally:
        with contextlib.suppress(Exception):
            if client is not None:
                client.close()
    response = bytes(response_buffer)
    try:
        headers, body = response.split(b"\r\n\r\n", 1)
        if b" 200 " not in headers.split(b"\r\n", 1)[0]:
            return 1
        payload = json.loads(body.decode("utf-8"))
        websocket_url = str(payload.get("webSocketDebuggerUrl") or "")
        parsed_websocket = urlsplit(websocket_url)
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return 1
    return 0 if (
        parsed_websocket.scheme == "ws"
        and (parsed_websocket.hostname or "").casefold() in {
            "127.0.0.1", "localhost",
        }
        and parsed_websocket.path.startswith("/devtools/browser/")
    ) else 1


def _http_response_has_complete_body(response: bytes | bytearray) -> bool:
    """Return whether a bounded HTTP response contains its declared body."""

    header_end = response.find(b"\r\n\r\n")
    if header_end < 0:
        return False
    content_length: int | None = None
    for header in response[:header_end].split(b"\r\n")[1:]:
        name, separator, value = header.partition(b":")
        if not separator or name.strip().lower() != b"content-length":
            continue
        if content_length is not None:
            return False
        try:
            content_length = int(value.strip())
        except ValueError:
            return False
        if not 0 <= content_length <= 128 * 1024:
            return False
    if content_length is None:
        return False
    return len(response) - header_end - 4 >= content_length


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        return healthcheck()
    try:
        return asyncio.run(serve())
    except Exception as exc:
        print(f"browser sidecar failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
