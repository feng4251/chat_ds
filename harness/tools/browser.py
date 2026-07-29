"""Browser automation tool using Playwright.

Provides browser_navigate, browser_snapshot, browser_click, browser_type,
browser_scroll, and browser_back. Sessions are isolated by session_id.

Key design:
  - Playwright async API (headless Chromium)
  - Version-neutral DOM extraction for page snapshots
  - Element ref system (@e1, @e2) for targeting click/type
  - URL safety on every browser request, including redirects and subresources
  - Exact per-run private-origin grants supplied only by ToolContext
  - Per-session browser contexts
  - Snapshot truncation at 8000 chars
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import secrets
from pathlib import Path
import stat
import time
from typing import Any
from urllib.parse import urlsplit

from config import settings
from tools.approval import canonical_http_origin, check_url_safety
from tools.context import ToolContext
from tools.execution_fence import require_execution_authority
from tools.session_sandbox_policy import (
    browser_context_egress_rules,
    browser_egress_request_allowed,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SNAPSHOT_TRUNCATE_THRESHOLD = 8000
_DOM_REF_ATTRIBUTE = "data-chatds-agent-ref"
_MAX_DOM_CONTROLS = 500

_BROWSER_CLEANUP_TIMEOUT = 300  # seconds of inactivity before cleanup
_CDP_RELAY_HANDSHAKE_TIMEOUT_SECONDS = 3.0
_CDP_RELAY_MAX_HEADER_BYTES = 64 * 1024
_CDP_RELAY_PATH_PREFIX = "/__chatds_cdp__/"

# This script is injected into every frame before page-authored JavaScript.
# Route interception covers ordinary HTTP(S) and WebSocket traffic; removing
# WebRTC/WebTransport closes browser-native transports that do not traverse a
# Playwright Route.  Chromium is independently started with QUIC disabled and
# a non-proxied-UDP WebRTC policy by the browser sidecar.
_BROWSER_TRANSPORT_GUARD_INIT_SCRIPT = r"""
(() => {
  "use strict";
  const blocked = (name) => function blockedBrowserTransport() {
    throw new DOMException(
      `${name} is disabled by browser network policy`,
      "SecurityError",
    );
  };
  for (const name of [
    "RTCPeerConnection",
    "webkitRTCPeerConnection",
    "mozRTCPeerConnection",
    "RTCDataChannel",
    "WebTransport",
  ]) {
    try {
      Object.defineProperty(globalThis, name, {
        value: blocked(name),
        writable: false,
        configurable: false,
        enumerable: false,
      });
    } catch (_) {
      try { globalThis[name] = blocked(name); } catch (_) {}
    }
  }
})();
"""

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

BROWSER_NAVIGATE_SCHEMA = {
    "name": "browser_navigate",
    "description": (
        "Navigate to a URL in the browser. Initializes the session and loads "
        "the page. Must be called before other browser tools. Returns a compact "
        "page snapshot with interactive elements and ref IDs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to (e.g., 'https://example.com')",
            },
        },
        "required": ["url"],
    },
}

BROWSER_SNAPSHOT_SCHEMA = {
    "name": "browser_snapshot",
    "description": (
        "Get a text-based snapshot of the current page's rendered DOM. "
        "Returns interactive elements with ref IDs (like @e1, @e2) for "
        "browser_click and browser_type. Snapshots over 8000 chars are truncated."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "full": {
                "type": "boolean",
                "description": "If true, returns complete page content. Default false (compact).",
                "default": False,
            },
        },
        "required": [],
    },
}

BROWSER_CLICK_SCHEMA = {
    "name": "browser_click",
    "description": (
        "Click on an element identified by its ref ID from the snapshot "
        "(e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The element reference from the snapshot (e.g., '@e5', '@e12')",
            },
        },
        "required": ["ref"],
    },
}

BROWSER_TYPE_SCHEMA = {
    "name": "browser_type",
    "description": (
        "Type text into an input field identified by its ref ID. "
        "Clears the field first, then types the new text."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The element reference from the snapshot (e.g., '@e3')",
            },
            "text": {
                "type": "string",
                "description": "The text to type into the field",
            },
        },
        "required": ["ref", "text"],
    },
}

BROWSER_SCROLL_SCHEMA = {
    "name": "browser_scroll",
    "description": "Scroll the page up or down to reveal more content.",
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Direction to scroll",
            },
        },
        "required": ["direction"],
    },
}

BROWSER_BACK_SCHEMA = {
    "name": "browser_back",
    "description": "Navigate back to the previous page in browser history.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

BrowserSessionKey = tuple[str, str, str]
_DIRECT_CONTEXT_USER = "__direct_contextless_browser__"
_DIRECT_CONTEXT_RUN = "__direct_contextless_run__"

# Browser state is isolated by runtime-owned user/session/run identity.  A
# model-facing argument can never select one of these mappings.
_sessions: dict[BrowserSessionKey, dict[str, Any]] = {}
_last_activity: dict[BrowserSessionKey, float] = {}
_browser_instance: Any = None  # shared browser process
_playwright_instance: Any = None  # retain driver lifecycle for the browser
_browser_relay: Any = None  # loopback TCP -> dedicated Unix socket bridge
_browser_lock = asyncio.Lock()


async def _relay_copy(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Copy one half of a CDP stream without retaining payloads or logging."""

    try:
        while True:
            data = await reader.read(64 * 1024)
            if not data:
                return
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        return


class _UnixSocketTcpRelay:
    """Expose one sidecar Unix socket on Harness loopback for Playwright.

    Playwright's Python client accepts HTTP(S) CDP endpoints but not Unix
    socket URLs.  This loopback-only relay preserves the private named-volume
    boundary without placing CDP on a Docker network or host port.
    """

    def __init__(self, socket_path: str):
        self.socket_path = Path(str(socket_path or ""))
        self.server: asyncio.AbstractServer | None = None
        self.endpoint_url: str | None = None
        self._authorized_request_target: str | None = None
        self._sidecar_websocket_path: str | None = None

    async def _discover_websocket_path(self) -> str:
        """Read Chromium discovery over UDS and return only its browser path.

        Chromium advertises a loopback address in ``webSocketDebuggerUrl``,
        but that loopback belongs to the sidecar.  Passing the HTTP discovery
        endpoint directly to Playwright would make the Harness later dial its
        own loopback.  We therefore validate the response here and preserve
        only the opaque browser path before rebinding it to this relay.
        """

        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.socket_path)
            )
            writer.write(
                b"GET /json/version HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            raw_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=3.0
            )
            if len(raw_headers) > 64 * 1024:
                raise RuntimeError("Browser CDP discovery headers are too large")
            header_lines = raw_headers[:-4].split(b"\r\n")
            if not header_lines or b" 200 " not in header_lines[0]:
                raise RuntimeError("Browser CDP discovery returned a non-200 status")
            headers: dict[bytes, bytes] = {}
            for raw_line in header_lines[1:]:
                if b":" not in raw_line:
                    raise RuntimeError("Browser CDP discovery headers are malformed")
                name, value = raw_line.split(b":", 1)
                name = name.strip().lower()
                if not name or name in headers:
                    raise RuntimeError("Browser CDP discovery headers are ambiguous")
                headers[name] = value.strip()
            if b"transfer-encoding" in headers:
                raise RuntimeError("Browser CDP discovery transfer encoding is unsupported")
            try:
                content_length = int(headers[b"content-length"])
            except (KeyError, ValueError) as exc:
                raise RuntimeError(
                    "Browser CDP discovery lacks a valid content length"
                ) from exc
            if not (1 <= content_length <= 128 * 1024):
                raise RuntimeError("Browser CDP discovery body size is invalid")
            body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=3.0
            )
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("Browser CDP discovery payload is not an object")
            advertised = str(payload.get("webSocketDebuggerUrl") or "")
            parsed = urlsplit(advertised)
            if (
                parsed.scheme != "ws"
                or (parsed.hostname or "").casefold()
                not in {"127.0.0.1", "localhost"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not re.fullmatch(
                    r"/devtools/browser/[A-Za-z0-9._~-]{1,200}",
                    parsed.path,
                )
            ):
                raise RuntimeError("Browser CDP discovery endpoint is unsafe")
            # Force port parsing now so malformed authorities fail closed even
            # though their sidecar-local value is deliberately discarded.
            # Chromium derives this URL from the HTTP Host header; our UDS
            # discovery deliberately sends ``Host: localhost``, so a missing
            # advertised port is both expected and safe.
            try:
                parsed.port
            except ValueError as exc:
                raise RuntimeError(
                    "Browser CDP discovery endpoint has an invalid port"
                ) from exc
            if parsed.netloc.endswith(":"):
                raise RuntimeError("Browser CDP discovery endpoint is unsafe")
            return parsed.path
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise RuntimeError("Browser CDP discovery response is truncated") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Browser CDP discovery payload is malformed") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _read_authorized_handshake(
        self,
        client_reader: asyncio.StreamReader,
    ) -> bytes | None:
        """Validate one exact, token-bearing WebSocket handshake.

        The relay does not connect to the privileged UDS until this returns a
        rewritten request.  In particular, neither the Chromium discovery
        route nor its raw browser path is reachable on Harness loopback.
        """

        try:
            raw = await asyncio.wait_for(
                client_reader.readuntil(b"\r\n\r\n"),
                timeout=_CDP_RELAY_HANDSHAKE_TIMEOUT_SECONDS,
            )
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
        ):
            return None
        if len(raw) > _CDP_RELAY_MAX_HEADER_BYTES:
            return None
        lines = raw[:-4].split(b"\r\n")
        if not lines:
            return None
        request_parts = lines[0].split(b" ")
        if len(request_parts) != 3:
            return None
        method, raw_target, version = request_parts
        try:
            request_target = raw_target.decode("ascii")
        except UnicodeError:
            return None
        authorized_target = self._authorized_request_target
        sidecar_target = self._sidecar_websocket_path
        if (
            method != b"GET"
            or version != b"HTTP/1.1"
            or not authorized_target
            or not sidecar_target
            or not hmac.compare_digest(request_target, authorized_target)
        ):
            return None

        headers: dict[bytes, bytes] = {}
        for line in lines[1:]:
            if (
                not line
                or line[:1] in {b" ", b"\t"}
                or b":" not in line
            ):
                return None
            name, value = line.split(b":", 1)
            name = name.strip().lower()
            value = value.strip()
            if (
                not name
                or name in headers
                or re.fullmatch(rb"[!#$%&'*+.^_`|~0-9a-z-]+", name) is None
            ):
                return None
            headers[name] = value
        connection_tokens = {
            token.strip().lower()
            for token in headers.get(b"connection", b"").split(b",")
        }
        if (
            headers.get(b"upgrade", b"").lower() != b"websocket"
            or b"upgrade" not in connection_tokens
            or headers.get(b"sec-websocket-version") != b"13"
            or not headers.get(b"sec-websocket-key")
            or b"transfer-encoding" in headers
            or b"content-length" in headers
        ):
            return None
        return (
            b"GET "
            + sidecar_target.encode("ascii")
            + b" HTTP/1.1\r\n"
            + b"\r\n".join(lines[1:])
            + b"\r\n\r\n"
        )

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _reject(self, client_writer: asyncio.StreamWriter) -> None:
        # Use one indistinguishable response for malformed and unauthorized
        # input, and disclose neither the valid path nor the sidecar state.
        with contextlib.suppress(ConnectionError, OSError):
            client_writer.write(
                b"HTTP/1.1 404 Not Found\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await client_writer.drain()
        await self._close_writer(client_writer)

    async def _handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        rewritten_handshake = await self._read_authorized_handshake(client_reader)
        if rewritten_handshake is None:
            await self._reject(client_writer)
            return
        try:
            sidecar_reader, sidecar_writer = await asyncio.open_unix_connection(
                str(self.socket_path)
            )
        except (ConnectionError, OSError):
            await self._close_writer(client_writer)
            return

        try:
            sidecar_writer.write(rewritten_handshake)
            await sidecar_writer.drain()
        except (ConnectionError, OSError):
            await self._close_writer(sidecar_writer)
            await self._close_writer(client_writer)
            return

        to_sidecar = asyncio.create_task(
            _relay_copy(client_reader, sidecar_writer)
        )
        from_sidecar = asyncio.create_task(
            _relay_copy(sidecar_reader, client_writer)
        )
        _done, pending = await asyncio.wait(
            {to_sidecar, from_sidecar},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(to_sidecar, from_sidecar, return_exceptions=True)
        await asyncio.gather(
            self._close_writer(sidecar_writer),
            self._close_writer(client_writer),
        )

    async def start(self) -> str:
        if not self.socket_path.is_absolute():
            raise RuntimeError("Browser CDP socket path must be absolute")
        try:
            mode = self.socket_path.lstat().st_mode
        except OSError as exc:
            raise RuntimeError("Browser CDP sidecar socket is unavailable") from exc
        if not stat.S_ISSOCK(mode):
            raise RuntimeError("Browser CDP control path is not a Unix socket")
        if self.server is not None:
            raise RuntimeError("Browser CDP loopback relay is already running")

        # Complete and validate privileged UDS discovery before binding any
        # TCP listener.  This removes the former startup interval in which a
        # loopback client could reach the raw sidecar proxy.
        websocket_path = await self._discover_websocket_path()
        relay_token = secrets.token_urlsafe(32)
        authorized_target = (
            f"{_CDP_RELAY_PATH_PREFIX}{relay_token}{websocket_path}"
        )
        self._sidecar_websocket_path = websocket_path
        self._authorized_request_target = authorized_target
        try:
            self.server = await asyncio.start_server(
                self._handle,
                host="127.0.0.1",
                port=0,
                limit=_CDP_RELAY_MAX_HEADER_BYTES,
            )
        except Exception:
            self._sidecar_websocket_path = None
            self._authorized_request_target = None
            raise
        sockets = self.server.sockets or ()
        if len(sockets) != 1:
            await self.close()
            raise RuntimeError("Browser CDP loopback relay failed to bind")
        host, port = sockets[0].getsockname()[:2]
        if host != "127.0.0.1":
            await self.close()
            raise RuntimeError("Browser CDP relay refused a non-loopback bind")
        self.endpoint_url = f"ws://127.0.0.1:{int(port)}{authorized_target}"
        return self.endpoint_url

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.endpoint_url = None
        self._authorized_request_target = None
        self._sidecar_websocket_path = None


def _browser_is_connected(browser: Any) -> bool:
    check = getattr(browser, "is_connected", None)
    if not callable(check):
        # Test doubles and older compatible clients need not expose the helper.
        return browser is not None
    try:
        return bool(check())
    except Exception:
        return False


async def _close_browser_transport() -> None:
    global _browser_instance, _playwright_instance, _browser_relay
    browser = _browser_instance
    playwright = _playwright_instance
    relay = _browser_relay
    sessions = list(_sessions.values())
    _sessions.clear()
    _last_activity.clear()
    _browser_instance = None
    _playwright_instance = None
    _browser_relay = None
    for session in sessions:
        context = session.get("context")
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            pass
    if relay is not None:
        try:
            await relay.close()
        except Exception:
            pass


async def close_all_browser_sessions() -> None:
    """Release every browser context and the shared sidecar transport."""

    async with _browser_lock:
        await _close_browser_transport()


def _browser_session_key(
    session_id: str,
    context: ToolContext | None,
) -> BrowserSessionKey:
    """Return the state key without trusting model-authored session identity."""

    if context is None:
        # Direct Python callers retain a compatibility bucket, but it carries
        # no private-origin grant and cannot collide with runtime contexts.
        return (
            _DIRECT_CONTEXT_USER,
            str(session_id or "default"),
            _DIRECT_CONTEXT_RUN,
        )
    require_execution_authority(
        context,
        boundary="browser.session_access",
    )
    user = str(context.user_id or "default")
    runtime_session = str(context.session_id or "default")
    runtime_run = str(
        context.run_id or context.browser_run_scope_id or ""
    )
    if not runtime_run:
        raise RuntimeError("Browser ToolContext lacks a runtime-owned run scope")
    return (user, runtime_session, runtime_run)


def _existing_session(
    session_key: BrowserSessionKey,
) -> dict[str, Any] | None:
    session = _sessions.get(session_key)
    if session is not None and not _browser_is_connected(session.get("browser")):
        # A restarted/crashed sidecar invalidates every CDP context.  Discard
        # the stale mapping instead of accidentally treating an old page as an
        # authorized live session; the next navigation reconnects explicitly.
        _sessions.pop(session_key, None)
        _last_activity.pop(session_key, None)
        return None
    if session is not None:
        _last_activity[session_key] = time.monotonic()
    return session


async def _get_browser():
    """Connect to the isolated browser sidecar, with no local fallback."""
    global _browser_instance, _playwright_instance, _browser_relay
    if _browser_instance is not None and not _browser_is_connected(
        _browser_instance
    ):
        await _close_browser_transport()
    if _browser_instance is None:
        from playwright.async_api import async_playwright

        socket_path = str(settings.browser_cdp_socket or "").strip()
        if not socket_path:
            raise RuntimeError("Browser CDP sidecar socket is not configured")
        relay = _UnixSocketTcpRelay(socket_path)
        pw = await async_playwright().start()
        try:
            endpoint_url = await relay.start()
            timeout_ms = max(
                1_000,
                min(
                    60_000,
                    int(float(settings.browser_cdp_connect_timeout_seconds) * 1000),
                ),
            )
            browser = await pw.chromium.connect_over_cdp(
                endpoint_url,
                timeout=timeout_ms,
            )
            if not _browser_is_connected(browser):
                raise RuntimeError("Browser sidecar returned a disconnected CDP client")
        except Exception as exc:
            await relay.close()
            await pw.stop()
            raise RuntimeError(
                "Browser sidecar CDP connection failed; local launch is disabled"
            ) from exc
        _browser_relay = relay
        _playwright_instance = pw
        _browser_instance = browser
    return _browser_instance


def _context_private_origins(
    context: ToolContext | None,
) -> tuple[str, ...]:
    """Project only private origins also present in this run's exact rules.

    Primary and delegated runs use the same rule.  Delegates receive an
    already-intersected child ledger from the Harness and never compile URLs
    from their own prompt, so intersecting that ledger with the parent's
    user/deployment private-origin grant preserves the complete capability
    chain without a blanket delegation denial.
    """

    if context is None:
        return ()
    exact_origins = {
        origin
        for prefix, _methods in browser_context_egress_rules(context)
        if (origin := canonical_http_origin(prefix)) is not None
    }
    return tuple(
        origin
        for raw_origin in context.allowed_browser_private_origins or ()
        if (
            (origin := canonical_http_origin(raw_origin)) is not None
            and origin == raw_origin
            and origin in exact_origins
        )
    )


def _bind_browser_policy(
    session: dict[str, Any],
    context: ToolContext | None,
) -> tuple[str, ...]:
    """Replace, rather than extend, the policy attached to a browser session."""

    require_execution_authority(
        context,
        boundary="browser.action_submit",
    )
    state_key = session.get("state_key")
    if not (
        isinstance(state_key, tuple)
        and len(state_key) == 3
        and all(isinstance(item, str) for item in state_key)
    ):
        raise RuntimeError("Browser session lacks a runtime-owned state key")
    expected_key = _browser_session_key(
        state_key[1] if context is None else context.session_id,
        context,
    )
    if state_key != expected_key:
        raise RuntimeError("Browser policy context does not own this session")
    allowed = _context_private_origins(context)
    egress_rules = browser_context_egress_rules(context)
    session["allowed_private_origins"] = allowed
    session["allowed_egress_rules"] = egress_rules
    session["policy_run_id"] = state_key[2]
    session["last_blocked_request"] = None
    return allowed


def browser_navigate_args_preflight(
    args: dict[str, Any],
    context: ToolContext | None,
) -> dict[str, Any] | None:
    """Reject model URLs outside the runtime-owned exact browser ledger."""

    url = args.get("url")
    rules = browser_context_egress_rules(context)
    if not isinstance(url, str) or not browser_egress_request_allowed(
        url,
        "GET",
        rules,
    ):
        return {
            "error": (
                "Browser navigation target is outside the runtime-owned "
                "method-and-URL egress policy."
            ),
            "reason": "browser_egress_policy_violation",
            "tool_name": "browser_navigate",
            "actual_dispatch_attempted": False,
        }
    return None


def _request_is_navigation(request: Any) -> bool:
    value = getattr(request, "is_navigation_request", False)
    try:
        return bool(value() if callable(value) else value)
    except Exception:
        return False


def _request_method(request: Any) -> str:
    value = getattr(request, "method", "")
    try:
        method = value() if callable(value) else value
    except Exception:
        return ""
    return str(method or "").upper()


def _blocked_url_label(url: str) -> str:
    """Describe a rejected target without reflecting URL credentials."""

    origin = canonical_http_origin(str(url or ""))
    return origin or "<invalid-or-non-http(s) URL>"


async def _check_browser_url_safety(
    url: str,
    *,
    allowed_private_origins: tuple[str, ...],
) -> str | None:
    """Run the DNS-backed URL guard without blocking the asyncio loop."""

    return await asyncio.to_thread(
        check_url_safety,
        url,
        allowed_private_origins=tuple(allowed_private_origins),
        synthetic_public_ranges=settings.browser_dns_synthetic_public_ranges,
    )


async def _guard_browser_request(
    session: dict[str, Any],
    route: Any,
    request: Any,
) -> None:
    """Revalidate every request before Chromium dispatches it.

    Checking subresources as well as document navigations prevents a public
    page from using fetch/iframes/images as a browser-side SSRF bridge.  The
    exact private-origin grant is read from the current runtime-bound session
    policy, so a redirect cannot widen it.
    """

    url = str(getattr(request, "url", "") or "")
    method = _request_method(request)
    if not browser_egress_request_allowed(
        url,
        method,
        tuple(session.get("allowed_egress_rules") or ()),
    ):
        error = (
            "Blocked: request method/URL is outside the runtime-owned "
            "browser egress policy"
        )
    else:
        error = await _check_browser_url_safety(
            url,
            allowed_private_origins=tuple(
                session.get("allowed_private_origins") or ()
            ),
        )
    if error:
        session["last_blocked_request"] = {
            "target": _blocked_url_label(url),
            "error": error,
            "navigation": _request_is_navigation(request),
            "method": method or "<unavailable>",
        }
        await route.abort("blockedbyclient")
        return
    if _request_is_navigation(request):
        session["current_navigation_method"] = method
    await route.continue_()


async def _guard_browser_websocket(
    session: dict[str, Any],
    web_socket_route: Any,
) -> None:
    """Block WebSocket transport before it connects to a server.

    ``page.route`` does not intercept WebSocket handshakes.  Private-origin
    grants deliberately authorize only exact HTTP(S) origins, so translating
    them into ambient ``ws://``/``wss://`` authority would widen the grant.
    SSE remains available because it uses an ordinary routed HTTP request.
    """

    session["last_blocked_request"] = {
        "target": "<non-http(s) WebSocket URL>",
        "error": (
            "Blocked: browser WebSocket transport is outside the exact "
            "HTTP(S) origin policy"
        ),
        "navigation": False,
        "transport": "websocket",
    }
    await web_socket_route.close(
        code=1008,
        reason="Blocked by browser network policy",
    )


async def _blank_unsafe_page(session: dict[str, Any]) -> None:
    page = session["page"]
    try:
        await page.goto("about:blank")
    except Exception:
        # ``about:blank`` is browser-internal and normally bypasses routing;
        # closing the context is too destructive for a recoverable block.
        pass
    session["current_url"] = None
    session["current_navigation_method"] = None


def _blocked_navigation_detail(session: dict[str, Any]) -> str | None:
    blocked = session.get("last_blocked_request")
    if not isinstance(blocked, dict) or not blocked.get("navigation"):
        return None
    return (
        f"{blocked.get('error') or 'unsafe navigation'} "
        f"(target origin: {blocked.get('target') or '<unknown>'})"
    )


async def _validate_current_page(
    session: dict[str, Any],
    allowed_private_origins: tuple[str, ...],
) -> str | None:
    """Fail closed if an action landed on an origin outside this turn's grant."""

    page = session["page"]
    current_url = str(getattr(page, "url", "") or "")
    if not current_url or current_url == "about:blank":
        return "No authorized page is currently loaded. Use browser_navigate first."
    error = await _check_browser_url_safety(
        current_url,
        allowed_private_origins=allowed_private_origins,
    )
    navigation_method = str(
        session.get("current_navigation_method") or "GET"
    ).upper()
    if not error and not browser_egress_request_allowed(
        current_url,
        navigation_method,
        tuple(session.get("allowed_egress_rules") or ()),
    ):
        error = (
            "Blocked: current page method/URL is outside the runtime-owned "
            "browser egress policy"
        )
    if error:
        target = _blocked_url_label(current_url)
        await _blank_unsafe_page(session)
        return f"{error} (target origin: {target})"
    session["current_url"] = current_url
    return None


async def _get_session(session_key: BrowserSessionKey) -> dict[str, Any]:
    """Get or create one browser context for an exact runtime state key."""
    # Per-run isolation creates more short-lived contexts than the historical
    # per-chat map. Opportunistically reap expired contexts before allocating
    # another one; every handler refreshes activity through `_existing_session`.
    await cleanup_inactive()
    existing = _existing_session(session_key)
    if existing is not None:
        return existing

    async with _browser_lock:
        existing = _existing_session(session_key)
        if existing is not None:
            return existing

        browser = await _get_browser()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            accept_downloads=False,
            # Route interception must not be bypassed by a page-controlled
            # service worker.
            service_workers="block",
        )
        session = {
            "state_key": session_key,
            "browser": browser,
            "context": context,
            "page": None,
            "current_url": None,
            "allowed_private_origins": (),
            "allowed_egress_rules": (),
            "policy_run_id": None,
            "last_blocked_request": None,
            "current_navigation_method": None,
        }

        async def request_guard(route, request):
            await _guard_browser_request(session, route, request)

        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            await context.close()
            raise RuntimeError(
                "Browser runtime lacks fail-closed context WebSocket routing support"
            )

        async def websocket_guard(web_socket_route):
            await _guard_browser_websocket(session, web_socket_route)

        try:
            # Context-level routes are installed before the first page exists;
            # they therefore cover popups, target=_blank, and future pages in
            # addition to the primary page.
            await context.add_init_script(
                script=_BROWSER_TRANSPORT_GUARD_INIT_SCRIPT,
            )
            await context.route("**/*", request_guard)
            await route_web_socket("**/*", websocket_guard)
            page = await context.new_page()
        except Exception:
            await context.close()
            raise
        session["page"] = page
        session["request_guard"] = request_guard
        session["websocket_guard"] = websocket_guard
        _sessions[session_key] = session
        _last_activity[session_key] = time.monotonic()
        return session


# ---------------------------------------------------------------------------
# DOM snapshot + stable element references
# ---------------------------------------------------------------------------

async def _build_snapshot(page, compact: bool = True) -> str:
    """Build a Playwright-version-neutral DOM snapshot with stable ref IDs."""
    try:
        payload = await page.evaluate(
            r"""({attribute, maxControls, textLimit}) => {
                const concise = (value, limit = 240) => String(value || '')
                  .replace(/\s+/g, ' ').trim().slice(0, limit);
                const visible = (element) => {
                  const style = getComputedStyle(element);
                  const rect = element.getBoundingClientRect();
                  return style.visibility !== 'hidden' && style.display !== 'none'
                    && rect.width > 0 && rect.height > 0;
                };
                document.querySelectorAll(`[${attribute}]`).forEach((element) => {
                  const value = element.getAttribute(attribute) || '';
                  if (/^e\d+$/.test(value)) element.removeAttribute(attribute);
                });
                const selector = [
                  'a[href]', 'button', 'input:not([type="hidden"])', 'textarea',
                  'select', '[role="button"]', '[role="link"]', '[role="textbox"]',
                  '[role="searchbox"]', '[role="combobox"]', '[role="checkbox"]',
                  '[role="radio"]', '[role="switch"]', '[role="tab"]',
                  '[contenteditable="true"]', 'summary'
                ].join(',');
                const roleFor = (element) => {
                  const explicit = element.getAttribute('role');
                  if (explicit) return explicit;
                  const tag = element.tagName.toLowerCase();
                  const type = (element.getAttribute('type') || '').toLowerCase();
                  if (tag === 'a') return 'link';
                  if (tag === 'button' || tag === 'summary') return 'button';
                  if (tag === 'select') return 'combobox';
                  if (tag === 'textarea' || element.isContentEditable) return 'textbox';
                  if (tag === 'input') {
                    if (type === 'checkbox') return 'checkbox';
                    if (type === 'radio') return 'radio';
                    if (type === 'search') return 'searchbox';
                    if (['button', 'submit', 'reset'].includes(type)) return 'button';
                    return 'textbox';
                  }
                  return tag;
                };
                const nameFor = (element) => {
                  const labels = element.labels
                    ? [...element.labels].map((item) => concise(item.innerText, 120)).filter(Boolean)
                    : [];
                  return concise(
                    element.getAttribute('aria-label') || labels.join(' | ')
                    || element.getAttribute('alt') || element.getAttribute('placeholder')
                    || element.getAttribute('title') || element.innerText
                    || element.textContent || element.getAttribute('name'),
                    180,
                  );
                };
                const controls = [...document.querySelectorAll(selector)]
                  .filter(visible).slice(0, maxControls).map((element, index) => {
                    const ref = `e${index + 1}`;
                    element.setAttribute(attribute, ref);
                    const password = (element.getAttribute('type') || '').toLowerCase() === 'password';
                    return {
                      ref, role: roleFor(element), name: nameFor(element),
                      value: password ? '[redacted]' : concise(element.value, 120),
                      href: concise(element.href, 300),
                      disabled: Boolean(element.disabled),
                      checked: 'checked' in element ? Boolean(element.checked) : null,
                    };
                  });
                return {
                  url: location.href,
                  title: document.title,
                  visibleText: String(document.body ? document.body.innerText : '')
                    .replace(/\s+$/g, '').slice(0, textLimit),
                  controls,
                };
              }""",
            {
                "attribute": _DOM_REF_ATTRIBUTE,
                "maxControls": _MAX_DOM_CONTROLS,
                "textLimit": 5_500 if compact else 20_000,
            },
        )
        if not isinstance(payload, dict):
            return "(No rendered DOM snapshot available)"
        lines = [
            f"document {str(payload.get('title') or '').strip()!r}",
            f"url {str(payload.get('url') or '').strip()}",
        ]
        visible_text = str(payload.get("visibleText") or "").strip()
        if visible_text:
            lines.extend(("visible text:", visible_text))
        controls = payload.get("controls")
        if isinstance(controls, list) and controls:
            lines.append("interactive controls:")
            for control in controls:
                if not isinstance(control, dict):
                    continue
                ref = str(control.get("ref") or "")
                role = str(control.get("role") or "control")
                name = str(control.get("name") or "")
                details: list[str] = []
                if control.get("value") not in {None, ""}:
                    details.append(f"value={control.get('value')!r}")
                if control.get("href"):
                    details.append(f"href={control.get('href')!r}")
                if control.get("disabled") is True:
                    details.append("disabled")
                if control.get("checked") is not None:
                    details.append(f"checked={bool(control.get('checked'))}")
                suffix = " " + " ".join(details) if details else ""
                lines.append(f"[@{ref}] {role} {name!r}{suffix}")

        snapshot_text = "\n".join(lines)

        # Truncate if needed
        if len(snapshot_text) > SNAPSHOT_TRUNCATE_THRESHOLD:
            split_at = snapshot_text.rfind("\n", 0, SNAPSHOT_TRUNCATE_THRESHOLD)
            if split_at < 0:
                split_at = SNAPSHOT_TRUNCATE_THRESHOLD
            remaining = len(snapshot_text) - split_at
            snapshot_text = (
                snapshot_text[:split_at]
                + f"\n\n[... {remaining} more chars truncated ...]"
            )

        return snapshot_text

    except Exception as e:
        logger.warning("Failed to build DOM snapshot: %s", e)
        # Fallback: extract text content
        try:
            text = await page.inner_text("body")
            return f"(DOM snapshot unavailable — page text content follows)\n\n{text[:SNAPSHOT_TRUNCATE_THRESHOLD]}"
        except Exception:
            return f"(Failed to get page content: {e})"


async def _click_element(page, ref_str: str) -> str | None:
    """Click the exact DOM element marked by the latest snapshot."""
    match = re.fullmatch(r"@?e([1-9]\d{0,3})", str(ref_str or ""))
    if not match:
        return f"Invalid ref '{ref_str}'. Use format like '@e5'."
    try:
        ref = f"e{match.group(1)}"
        locator = page.locator(f'[{_DOM_REF_ATTRIBUTE}="{ref}"]')
        count = await locator.count()
        if count != 1:
            return (
                f"Ref @{ref} is stale or ambiguous (matched {count} elements). "
                "Take a fresh browser_snapshot and use its ref."
            )
        await locator.click()
        return None
    except Exception as e:
        return f"Click failed: {e}"


async def _type_text(page, ref_str: str, text: str) -> str | None:
    """Type text into an input element by its ref ID."""
    match = re.fullmatch(r"@?e([1-9]\d{0,3})", str(ref_str or ""))
    if not match:
        return f"Invalid ref '{ref_str}'. Use format like '@e5'."

    try:
        ref = f"e{match.group(1)}"
        locator = page.locator(f'[{_DOM_REF_ATTRIBUTE}="{ref}"]')
        count = await locator.count()
        if count != 1:
            return (
                f"Ref @{ref} is stale or ambiguous (matched {count} elements). "
                "Take a fresh browser_snapshot and use its ref."
            )
        await locator.fill(text)
        return None
    except Exception as e:
        return f"Type failed: {e}"


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

async def _cleanup_session_locked(
    session_key: BrowserSessionKey,
    *,
    close_transport_if_idle: bool,
) -> None:
    """Remove one exact context while the caller holds ``_browser_lock``."""

    session = _sessions.pop(session_key, None)
    _last_activity.pop(session_key, None)
    if session:
        context = session.get("context")
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
    if close_transport_if_idle and not _sessions:
        await _close_browser_transport()


async def cleanup_session(session_key: BrowserSessionKey) -> None:
    """Clean up browser resources for one exact runtime state key."""

    async with _browser_lock:
        await _cleanup_session_locked(
            session_key,
            close_transport_if_idle=False,
        )


async def close_browser_run(
    user_id: str,
    session_id: str,
    run_id: str,
) -> None:
    """Close exactly one runtime-owned browser context.

    Cleanup is allowed to finish even if its request/stream task is cancelled;
    cancellation is re-raised only after the context and, when this was the
    last run, the shared CDP transport have been released.
    """

    normalized_run = str(run_id or "")
    if not normalized_run:
        return
    session_key: BrowserSessionKey = (
        str(user_id or "default"),
        str(session_id or "default"),
        normalized_run,
    )

    async def cleanup() -> None:
        async with _browser_lock:
            await _cleanup_session_locked(
                session_key,
                close_transport_if_idle=True,
            )

    cleanup_task = asyncio.create_task(cleanup())
    pending_cancellation: asyncio.CancelledError | None = None
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exc:
            # A client disconnect can cancel the streaming response more than
            # once.  Preserve that signal, but keep shielding the ownership
            # cleanup until its exact context is gone.
            pending_cancellation = exc
    # Surface an unexpected cleanup failure before preserving cancellation.
    cleanup_task.result()
    if pending_cancellation is not None:
        raise pending_cancellation


async def cleanup_inactive():
    """Clean up sessions that have been inactive beyond the timeout."""
    now = time.monotonic()
    stale = [
        session_key for session_key, ts in _last_activity.items()
        if now - ts > _BROWSER_CLEANUP_TIMEOUT
    ]
    for session_key in stale:
        async with _browser_lock:
            # Another coroutine may have reused this exact run after the stale
            # snapshot. Revalidate while holding the same lock used for map
            # removal and context allocation.
            latest = _last_activity.get(session_key)
            if latest is None or now - latest <= _BROWSER_CLEANUP_TIMEOUT:
                continue
            # Do not put tenant/run identifiers in ordinary logs.
            logger.info("Cleaning up one inactive isolated browser context")
            await _cleanup_session_locked(
                session_key,
                close_transport_if_idle=False,
            )


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def browser_navigate(
    url: str,
    session_id: str = "default",
    context: ToolContext | None = None,
) -> str:
    """Navigate to a URL. Returns snapshot of the loaded page."""
    try:
        session_key = _browser_session_key(session_id, context)
    except RuntimeError as exc:
        return f"Browser navigation blocked: {exc}"
    allowed = _context_private_origins(context)
    egress_rules = browser_context_egress_rules(context)
    existing = _existing_session(session_key)
    if existing is not None:
        # A new tool call must revoke any ledger left by an earlier run before
        # even validating its new model-supplied argument.
        _bind_browser_policy(existing, context)
    if not browser_egress_request_allowed(url, "GET", egress_rules):
        return (
            "Browser navigation blocked: target is outside the runtime-owned "
            "method-and-URL egress policy"
        )
    err = await _check_browser_url_safety(
        url,
        allowed_private_origins=allowed,
    )
    if err:
        return f"Browser navigation blocked: {err}"

    try:
        session = await _get_session(session_key)
        allowed = _bind_browser_policy(session, context)
        page = session["page"]

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as exc:
            blocked = _blocked_navigation_detail(session)
            if blocked:
                await _blank_unsafe_page(session)
                return f"Browser navigation blocked: {blocked}"
            return f"Browser navigate error: {exc}"

        # Route interception checks the initial request and every redirect
        # before dispatch.  This final check also catches browser-internal URL
        # changes that do not produce a routable request.
        final_error = await _validate_current_page(session, allowed)
        if final_error:
            return f"Browser navigation blocked: {final_error}"
        final_url = str(page.url)

        # Build snapshot
        snapshot = await _build_snapshot(page)

        return (
            f"Navigated to: {final_url}\n"
            f"--- Page Snapshot ---\n"
            f"{snapshot}\n"
            f"--- End Snapshot ---"
        )

    except Exception as e:
        return f"Browser navigate error: {e}"


async def browser_snapshot(
    full: bool = False,
    session_id: str = "default",
    context: ToolContext | None = None,
) -> str:
    """Return a snapshot of the current page."""
    try:
        session_key = _browser_session_key(session_id, context)
    except RuntimeError as exc:
        return f"Browser snapshot blocked: {exc}"
    session = _existing_session(session_key)
    if not session:
        return "No active browser session. Use browser_navigate first."

    try:
        allowed = _bind_browser_policy(session, context)
        page_error = await _validate_current_page(session, allowed)
        if page_error:
            return f"Browser snapshot blocked: {page_error}"
        page = session["page"]
        snapshot = await _build_snapshot(page, compact=not full)
        url = page.url

        return (
            f"Current URL: {url}\n"
            f"--- Page Snapshot ---\n"
            f"{snapshot}\n"
            f"--- End Snapshot ---"
        )

    except Exception as e:
        return f"Browser snapshot error: {e}"


async def browser_click(
    ref: str,
    session_id: str = "default",
    context: ToolContext | None = None,
) -> str:
    """Click an element by ref ID."""
    try:
        session_key = _browser_session_key(session_id, context)
    except RuntimeError as exc:
        return f"Browser click blocked: {exc}"
    session = _existing_session(session_key)
    if not session:
        return "No active browser session. Use browser_navigate first."

    allowed = _bind_browser_policy(session, context)
    page_error = await _validate_current_page(session, allowed)
    if page_error:
        return f"Browser click blocked: {page_error}"
    page = session["page"]

    err = await _click_element(page, ref)
    if err:
        blocked = _blocked_navigation_detail(session)
        if blocked:
            return f"Browser click navigation blocked: {blocked}"
        return err

    # Wait a bit for page to react
    await asyncio.sleep(0.5)

    blocked = _blocked_navigation_detail(session)
    if blocked:
        return f"Browser click navigation blocked: {blocked}"
    page_error = await _validate_current_page(session, allowed)
    if page_error:
        return f"Browser click navigation blocked: {page_error}"

    # Return updated snapshot
    snapshot = await _build_snapshot(page)
    url = page.url
    session["current_url"] = url

    return (
        f"Clicked {ref}\n"
        f"Current URL: {url}\n"
        f"--- Page Snapshot ---\n"
        f"{snapshot}\n"
        f"--- End Snapshot ---"
    )


async def browser_type(
    ref: str,
    text: str,
    session_id: str = "default",
    context: ToolContext | None = None,
) -> str:
    """Type text into an input field by ref ID."""
    try:
        session_key = _browser_session_key(session_id, context)
    except RuntimeError as exc:
        return f"Browser type blocked: {exc}"
    session = _existing_session(session_key)
    if not session:
        return "No active browser session. Use browser_navigate first."

    allowed = _bind_browser_policy(session, context)
    page_error = await _validate_current_page(session, allowed)
    if page_error:
        return f"Browser type blocked: {page_error}"
    page = session["page"]

    err = await _type_text(page, ref, text)
    if err:
        blocked = _blocked_navigation_detail(session)
        if blocked:
            return f"Browser type navigation blocked: {blocked}"
        return err

    blocked = _blocked_navigation_detail(session)
    if blocked:
        return f"Browser type navigation blocked: {blocked}"
    page_error = await _validate_current_page(session, allowed)
    if page_error:
        return f"Browser type navigation blocked: {page_error}"
    return f"Typed text into {ref}"


async def browser_scroll(
    direction: str,
    session_id: str = "default",
    context: ToolContext | None = None,
) -> str:
    """Scroll the page."""
    try:
        session_key = _browser_session_key(session_id, context)
    except RuntimeError as exc:
        return f"Browser scroll blocked: {exc}"
    session = _existing_session(session_key)
    if not session:
        return "No active browser session. Use browser_navigate first."

    allowed = _bind_browser_policy(session, context)
    page_error = await _validate_current_page(session, allowed)
    if page_error:
        return f"Browser scroll blocked: {page_error}"
    page = session["page"]

    try:
        if direction == "down":
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.7)")
        else:
            await page.evaluate("window.scrollBy(0, -window.innerHeight * 0.7)")

        await asyncio.sleep(0.3)

        blocked = _blocked_navigation_detail(session)
        if blocked:
            return f"Browser scroll navigation blocked: {blocked}"
        page_error = await _validate_current_page(session, allowed)
        if page_error:
            return f"Browser scroll navigation blocked: {page_error}"
        snapshot = await _build_snapshot(page)
        return (
            f"Scrolled {direction}\n"
            f"--- Page Snapshot ---\n"
            f"{snapshot}\n"
            f"--- End Snapshot ---"
        )
    except Exception as e:
        return f"Browser scroll error: {e}"


async def browser_back(
    session_id: str = "default",
    context: ToolContext | None = None,
) -> str:
    """Navigate back."""
    try:
        session_key = _browser_session_key(session_id, context)
    except RuntimeError as exc:
        return f"Browser back blocked: {exc}"
    session = _existing_session(session_key)
    if not session:
        return "No active browser session. Use browser_navigate first."

    allowed = _bind_browser_policy(session, context)
    page_error = await _validate_current_page(session, allowed)
    if page_error:
        return f"Browser back blocked: {page_error}"
    page = session["page"]

    try:
        await page.go_back(wait_until="domcontentloaded", timeout=15000)
        blocked = _blocked_navigation_detail(session)
        if blocked:
            return f"Browser back navigation blocked: {blocked}"
        page_error = await _validate_current_page(session, allowed)
        if page_error:
            return f"Browser back navigation blocked: {page_error}"

        snapshot = await _build_snapshot(page)
        return (
            f"Navigated back to: {page.url}\n"
            f"--- Page Snapshot ---\n"
            f"{snapshot}\n"
            f"--- End Snapshot ---"
        )
    except Exception as e:
        blocked = _blocked_navigation_detail(session)
        if blocked:
            return f"Browser back navigation blocked: {blocked}"
        return f"Browser back error: {e}"
