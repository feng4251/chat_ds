"""Tool result storage — cap & persist overflow tool outputs.

In a multi-user web environment, tool results flowing through the conversation
must be bounded.  Large outputs (e.g. a 200 KB web page extract) are written to
the per-user per-session ``results/`` sandbox directory, and the conversation
message is replaced with a compact summary pointing to the persisted file.

Per-tool caps (characters):
  - ``execute_code`` ……  50 000  (50 KB)
  - ``web_extract``  …… 100 000 (100 KB)
  - ``browser_snapshot`` … 8 000   (8 KB)
  - ``read_file``    …… 100 000 (100 KB)
  -  everything else …… 50 000  (50 KB)

The persisted file path is always relative to the sandbox root so the model
can read it back later with ``read_file`` if needed.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from tools.path_security import sandbox_dir

logger = logging.getLogger(__name__)

# Per-tool character caps  (None = no overflow persistence needed)
_TOOL_CAPS: dict[str, int] = {
    "execute_code": 50_000,
    "web_extract": 100_000,
    "browser_snapshot": 8_000,
    "read_file": 100_000,
    "web_search": 20_000,
}

_DEFAULT_CAP = 50_000

# Maximum file size we will write to disk (safety net)
_MAX_PERSIST_BYTES = 5 * 1024 * 1024  # 5 MB


def _cap_for_tool(tool_name: str) -> int:
    """Return the character cap for a given tool name."""
    return _TOOL_CAPS.get(tool_name, _DEFAULT_CAP)


def _persist(
    tool_name: str,
    full_result: str,
    user_id: str,
    session_id: str,
) -> str:
    """Write the full tool result to disk and return a relative path."""
    results_dir = sandbox_dir(user_id, session_id, sub="results")
    ts = int(time.time() * 1_000_000)
    safe_name = "".join(c if c.isalnum() or c in "_-." else "_" for c in tool_name)
    filename = f"{safe_name}_{ts}.txt"
    filepath = results_dir / filename

    try:
        # Truncate to max persist size
        data = full_result
        if len(data) > _MAX_PERSIST_BYTES:
            data = data[:_MAX_PERSIST_BYTES] + "\n\n[... truncated at 5 MB safety limit]"
        filepath.write_text(data, encoding="utf-8")
        logger.info("Persisted %s result (%d chars) → %s", tool_name, len(full_result), filename)
    except Exception as exc:
        logger.exception("Failed to persist tool result for %s: %s", tool_name, exc)
        return f"[Result too large to persist — {exc}]"

    # Return a path relative to the sandbox root so read_file can find it
    return f"results/{filename}"


def wrap_result(
    raw_result: str,
    tool_name: str,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Wrap a tool dispatch result — persist overflow and return bounded output.

    This should be called in the agent loop immediately after ``dispatch()``
    and before the result is appended to the conversation.

    Args:
        raw_result: The raw string returned by the tool handler.
        tool_name: Name of the tool (e.g. ``"execute_code"``).
        user_id: User identifier for sandbox isolation.
        session_id: Session identifier for sandbox isolation.

    Returns:
        A (possibly truncated) result string safe to inject into the
        conversation.  Overflow is persisted to ``results/`` in the session
        sandbox, and the message includes a reference so the model can
        ``read_file`` the persisted file.
    """
    cap = _cap_for_tool(tool_name)
    result_len = len(raw_result)

    if result_len <= cap:
        return raw_result

    # Persist the full result and return a truncated summary
    persisted_path = _persist(tool_name, raw_result, user_id, session_id)

    # Truncate to cap chars, keeping the beginning (most relevant part)
    truncated = raw_result[:cap]

    summary = (
        f"{truncated}\n\n"
        f"[... {result_len - cap} more chars truncated ...]\n"
        f"[Full result persisted to sandbox: {persisted_path} — "
        f"use read_file({json.dumps(persisted_path)}) to retrieve]"
    )

    # Ensure the summary itself doesn't exceed cap by too much
    if len(summary) > cap + 2000:
        summary = summary[:cap + 2000]

    return summary


def get_tool_caps() -> dict[str, int]:
    """Return a copy of the per-tool cap configuration (for debugging)."""
    return dict(_TOOL_CAPS)