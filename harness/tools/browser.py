"""Browser automation tool using Playwright.

Provides browser_navigate, browser_snapshot, browser_click, browser_type,
browser_scroll, and browser_back. Sessions are isolated by session_id.

Key design:
  - Playwright async API (headless Chromium)
  - Accessibility tree extraction for page snapshots
  - Element ref system (@e1, @e2) for targeting click/type
  - URL safety: delegates to shared approval.check_url_safety
  - Per-session browser contexts
  - Snapshot truncation at 8000 chars
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from tools.approval import check_url_safety

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SNAPSHOT_TRUNCATE_THRESHOLD = 8000

_BROWSER_CLEANUP_TIMEOUT = 300  # seconds of inactivity before cleanup

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
        "Get a text-based snapshot of the current page's accessibility tree. "
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

# Per-session browser state: session_id -> {browser, context, page, ...}
_sessions: dict[str, dict[str, Any]] = {}
_last_activity: dict[str, float] = {}
_browser_instance: Any = None  # shared browser process
_browser_lock = asyncio.Lock()


async def _get_browser():
    """Get or create the shared Playwright browser instance."""
    global _browser_instance
    if _browser_instance is None:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        _browser_instance = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
    return _browser_instance


async def _get_session(session_id: str) -> dict[str, Any]:
    """Get or create a browser context + page for the given session."""
    if session_id in _sessions:
        _last_activity[session_id] = time.monotonic()
        return _sessions[session_id]

    async with _browser_lock:
        if session_id in _sessions:
            return _sessions[session_id]

        browser = await _get_browser()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        session = {
            "browser": browser,
            "context": context,
            "page": page,
            "current_url": None,
            "element_map": {},  # ref_id -> locator strategy
            "next_ref_id": 1,
        }
        _sessions[session_id] = session
        _last_activity[session_id] = time.monotonic()
        return session


# ---------------------------------------------------------------------------
# Accessibility tree + element mapping
# ---------------------------------------------------------------------------

async def _build_snapshot(page, compact: bool = True) -> str:
    """Build a text-based accessibility snapshot of the current page.

    Maps interactive elements to ref IDs ([@e1], [@e2], ...) so the model
    can reference them in subsequent browser_click / browser_type calls.
    """
    try:
        # Get accessibility tree from Playwright
        snapshot = await page.accessibility.snapshot()
        if not snapshot:
            return "(No accessibility tree available)"

        lines = []
        element_map = {}

        def _walk(node, depth: int = 0, ref_id: int | None = None):
            indent = "  " * depth
            role = node.get("role", "unknown")
            name = node.get("name", "")
            value = node.get("value", "")
            description = node.get("description", "")

            # Build display line
            parts = [role]
            if name:
                parts.append(f'"{name}"')
            if value:
                parts.append(f"={value}")
            if description:
                parts.append(f"({description})")

            line = indent + " ".join(parts)

            # Assign ref IDs to interactive elements
            interactive_roles = {
                "button", "link", "textbox", "searchbox", "combobox",
                "listbox", "menuitem", "menuitemcheckbox", "menuitemradio",
                "option", "radio", "checkbox", "switch", "tab", "slider",
                "spinbutton", "textbox", "searchbox",
            }
            is_interactive = (
                role in interactive_roles
                or (role == "textbox" and node.get("focused"))
            )

            if is_interactive:
                ref_str = f" [@e{ref_id}]"
                line += ref_str
            elif not compact:
                # In full mode, still mark interactive elements
                pass

            lines.append(line)

            # Walk children
            for child_idx, child in enumerate(node.get("children", [])):
                child_ref = ref_id if not is_interactive else None
                _walk(child, depth + 1, child_ref)

        _walk(snapshot, ref_id=1)

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
        logger.warning("Failed to build accessibility snapshot: %s", e)
        # Fallback: extract text content
        try:
            text = await page.inner_text("body")
            return f"(Accessibility tree unavailable — page text content follows)\n\n{text[:SNAPSHOT_TRUNCATE_THRESHOLD]}"
        except Exception:
            return f"(Failed to get page content: {e})"


async def _click_element(page, ref_str: str) -> str | None:
    """Click an element by its accessibility ref ID."""
    # Parse ref: @e5 -> find the 5th interactive element
    match = re.match(r"@?e(\d+)", ref_str)
    if not match:
        return f"Invalid ref '{ref_str}'. Use format like '@e5'."

    idx = int(match.group(1))

    try:
        snapshot = await page.accessibility.snapshot()
        if not snapshot:
            return "No accessibility tree available"

        interactive_elements = []

        def _collect(node):
            role = node.get("role", "")
            interactive_roles = {
                "button", "link", "textbox", "searchbox", "combobox",
                "listbox", "menuitem", "option", "radio", "checkbox",
                "switch", "tab", "slider", "spinbutton",
            }
            if role in interactive_roles:
                interactive_elements.append(node)
            for child in node.get("children", []):
                _collect(child)

        _collect(snapshot)

        if idx < 1 or idx > len(interactive_elements):
            return f"Ref @e{idx} out of range (1-{len(interactive_elements)} interactive elements on page)"

        element = interactive_elements[idx - 1]
        name = element.get("name", "")
        role = element.get("role", "")

        # Try to find and click the element via locator strategies
        if name:
            # Try role + name locator
            try:
                locator = page.get_by_role(role, name=name)
                if await locator.count() > 0:
                    await locator.first.click()
                    return None
            except Exception:
                pass

            # Try text content locator
            try:
                locator = page.get_by_text(name, exact=True)
                if await locator.count() > 0:
                    await locator.first.click()
                    return None
            except Exception:
                pass

            # Try placeholder
            if role in ("textbox", "searchbox"):
                try:
                    locator = page.get_by_placeholder(name)
                    if await locator.count() > 0:
                        await locator.first.click()
                        return None
                except Exception:
                    pass

            # Try label
            try:
                locator = page.get_by_label(name)
                if await locator.count() > 0:
                    await locator.first.click()
                    return None
            except Exception:
                pass

        return f"Clicked element @e{idx} ({role}: '{name}')"

    except Exception as e:
        return f"Click failed: {e}"


async def _type_text(page, ref_str: str, text: str) -> str | None:
    """Type text into an input element by its ref ID."""
    match = re.match(r"@?e(\d+)", ref_str)
    if not match:
        return f"Invalid ref '{ref_str}'. Use format like '@e5'."

    idx = int(match.group(1))

    try:
        snapshot = await page.accessibility.snapshot()
        if not snapshot:
            return "No accessibility tree available"

        interactive_elements = []

        def _collect(node):
            role = node.get("role", "")
            interactive_roles = {
                "button", "link", "textbox", "searchbox", "combobox",
                "listbox", "menuitem", "option", "radio", "checkbox",
                "switch", "tab", "slider", "spinbutton",
            }
            if role in interactive_roles:
                interactive_elements.append(node)
            for child in node.get("children", []):
                _collect(child)

        _collect(snapshot)

        if idx < 1 or idx > len(interactive_elements):
            return f"Ref @e{idx} out of range (1-{len(interactive_elements)} interactive elements on page)"

        element = interactive_elements[idx - 1]
        name = element.get("name", "")
        role = element.get("role", "")

        # Find the input element
        if name:
            locator = None
            try:
                locator = page.get_by_role(role, name=name)
                if await locator.count() == 0:
                    locator = None
            except Exception:
                pass

            if not locator:
                try:
                    locator = page.get_by_placeholder(name)
                    if await locator.count() == 0:
                        locator = None
                except Exception:
                    pass

            if not locator:
                try:
                    locator = page.get_by_label(name)
                    if await locator.count() == 0:
                        locator = None
                except Exception:
                    pass

            if locator:
                await locator.first.fill(text)
                return None

        return f"Could not find input element @e{idx} to type into"
    except Exception as e:
        return f"Type failed: {e}"


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

async def cleanup_session(session_id: str):
    """Clean up browser resources for a session."""
    session = _sessions.pop(session_id, None)
    _last_activity.pop(session_id, None)
    if session:
        try:
            await session["context"].close()
        except Exception:
            pass


async def cleanup_inactive():
    """Clean up sessions that have been inactive beyond the timeout."""
    now = time.monotonic()
    stale = [
        sid for sid, ts in _last_activity.items()
        if now - ts > _BROWSER_CLEANUP_TIMEOUT
    ]
    for sid in stale:
        logger.info("Cleaning up inactive browser session: %s", sid)
        await cleanup_session(sid)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def browser_navigate(url: str, session_id: str = "default") -> str:
    """Navigate to a URL. Returns snapshot of the loaded page."""
    # URL safety check
    err = check_url_safety(url)
    if err:
        return f"Browser navigation blocked: {err}"

    try:
        session = await _get_session(session_id)
        page = session["page"]

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        session["current_url"] = page.url

        # Check if we were redirected to an unsafe URL
        final_url = page.url
        if final_url != url:
            if check_url_safety(final_url):
                await page.goto("about:blank")
                return (
                    f"Browser navigation blocked: redirected to unsafe URL "
                    f"({final_url[:200]}). Navigated to about:blank instead."
                )

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


async def browser_snapshot(full: bool = False, session_id: str = "default") -> str:
    """Return a snapshot of the current page."""
    session = _sessions.get(session_id)
    if not session:
        return "No active browser session. Use browser_navigate first."

    try:
        page = session["page"]
        snapshot = await _build_snapshot(page, compact=not full)
        url = session.get("current_url", page.url)

        return (
            f"Current URL: {url}\n"
            f"--- Page Snapshot ---\n"
            f"{snapshot}\n"
            f"--- End Snapshot ---"
        )

    except Exception as e:
        return f"Browser snapshot error: {e}"


async def browser_click(ref: str, session_id: str = "default") -> str:
    """Click an element by ref ID."""
    session = _sessions.get(session_id)
    if not session:
        return "No active browser session. Use browser_navigate first."

    page = session["page"]

    err = await _click_element(page, ref)
    if err:
        return err

    # Wait a bit for page to react
    await asyncio.sleep(0.5)

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


async def browser_type(ref: str, text: str, session_id: str = "default") -> str:
    """Type text into an input field by ref ID."""
    session = _sessions.get(session_id)
    if not session:
        return "No active browser session. Use browser_navigate first."

    page = session["page"]

    err = await _type_text(page, ref, text)
    if err:
        return err

    return f"Typed '{text}' into {ref}"


async def browser_scroll(direction: str, session_id: str = "default") -> str:
    """Scroll the page."""
    session = _sessions.get(session_id)
    if not session:
        return "No active browser session. Use browser_navigate first."

    page = session["page"]

    try:
        if direction == "down":
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.7)")
        else:
            await page.evaluate("window.scrollBy(0, -window.innerHeight * 0.7)")

        await asyncio.sleep(0.3)

        snapshot = await _build_snapshot(page)
        return (
            f"Scrolled {direction}\n"
            f"--- Page Snapshot ---\n"
            f"{snapshot}\n"
            f"--- End Snapshot ---"
        )
    except Exception as e:
        return f"Browser scroll error: {e}"


async def browser_back(session_id: str = "default") -> str:
    """Navigate back."""
    session = _sessions.get(session_id)
    if not session:
        return "No active browser session. Use browser_navigate first."

    page = session["page"]

    try:
        await page.go_back(wait_until="domcontentloaded", timeout=15000)
        session["current_url"] = page.url

        snapshot = await _build_snapshot(page)
        return (
            f"Navigated back to: {page.url}\n"
            f"--- Page Snapshot ---\n"
            f"{snapshot}\n"
            f"--- End Snapshot ---"
        )
    except Exception as e:
        return f"Browser back error: {e}"