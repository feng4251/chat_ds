"""Tool result storage — losslessly spill oversized tool outputs.

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

The persisted payload is outside the model-visible workspace.  The model gets
an opaque, runtime-granted handle and can read bounded character slices through
``read_tool_result``.  This keeps large/minified JSON lossless without granting
ambient filesystem access.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path

from tools.path_security import sandbox_dir
from tools.workspace_lock import workspace_mutation_guard

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
TOOL_RESULT_HANDLE_PREFIX = "tool-result:"
_TOOL_RESULT_FILENAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def _cap_for_tool(tool_name: str) -> int:
    """Return the character cap for a given tool name."""
    return _TOOL_CAPS.get(tool_name, _DEFAULT_CAP)


def _persist_path(
    tool_name: str,
    full_result: str,
    user_id: str,
    session_id: str,
    *,
    require_lossless: bool = True,
) -> str | None:
    """Write the full tool result and return its session-relative path."""
    # ``results`` is outside the model-visible workspace, but it belongs to
    # the same session transaction.  Use the exact sibling workspace lock so
    # Backend deletion/tombstone publication and every Harness commit have one
    # linearization point.  Keep lock and deletion-fence failures outside the
    # best-effort I/O handler: a detached run must stop rather than silently
    # continue after its session has been deleted.
    workspace = sandbox_dir(user_id, session_id, sub="workspace")
    with workspace_mutation_guard(workspace):
        results_dir = sandbox_dir(user_id, session_id, sub="results")
        safe_name = "".join(
            c if c in _TOOL_RESULT_FILENAME_CHARS else "_"
            for c in tool_name
        )[:96] or "tool"
        filename = f"{safe_name}_{uuid.uuid4().hex}.txt"
        filepath = results_dir / filename
        temporary: Path | None = None

        try:
            data = full_result
            encoded = data.encode("utf-8")
            if len(encoded) > _MAX_PERSIST_BYTES:
                if require_lossless:
                    logger.warning(
                        "Refused non-lossless %s spill above %d bytes",
                        tool_name,
                        _MAX_PERSIST_BYTES,
                    )
                    return None
                marker = b"\n\n[... truncated at 5 MB safety limit]"
                data = (
                    encoded[:_MAX_PERSIST_BYTES - len(marker)]
                    .decode("utf-8", errors="ignore")
                    + marker.decode("ascii")
                )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{filename}.",
                suffix=".tmp",
                dir=results_dir,
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            os.replace(temporary, filepath)
            temporary = None
            directory_fd = os.open(
                results_dir,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            logger.info(
                "Persisted %s result (%d chars) → %s",
                tool_name,
                len(full_result),
                filename,
            )
        except Exception as exc:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            logger.exception(
                "Failed to persist tool result for %s: %s",
                tool_name,
                exc,
            )
            return None

    # The path remains runtime-only; the model receives an opaque handle.
    return f"results/{filename}"


def _handle_for_path(path: str | None) -> str | None:
    if not isinstance(path, str) or not path.startswith("results/"):
        return None
    filename = path.removeprefix("results/")
    if not filename or Path(filename).name != filename:
        return None
    return TOOL_RESULT_HANDLE_PREFIX + filename


def result_path_for_handle(handle: str) -> str | None:
    """Decode one syntactically valid spill handle to a relative path.

    This function does not grant access.  ``read_tool_result`` additionally
    requires exact membership in the runtime-owned handle ledger.
    """

    if not isinstance(handle, str) or not handle.startswith(
        TOOL_RESULT_HANDLE_PREFIX
    ):
        return None
    filename = handle.removeprefix(TOOL_RESULT_HANDLE_PREFIX)
    if (
        not filename
        or Path(filename).name != filename
        or any(c not in _TOOL_RESULT_FILENAME_CHARS for c in filename)
    ):
        return None
    return f"results/{filename}"


def persist_tool_result_spill(
    raw_result: str,
    tool_name: str,
    user_id: str = "default",
    session_id: str = "default",
) -> str | None:
    """Persist a complete payload and return an opaque readback handle."""

    return _handle_for_path(
        _persist_path(tool_name, raw_result, user_id, session_id)
    )


def wrap_result_with_receipt(
    raw_result: str,
    tool_name: str,
    user_id: str = "default",
    session_id: str = "default",
) -> tuple[str, str | None]:
    """Return bounded model content plus any lossless spill handle."""

    cap = _cap_for_tool(tool_name)
    result_len = len(raw_result)

    if result_len <= cap:
        return raw_result, None

    handle = persist_tool_result_spill(
        raw_result,
        tool_name,
        user_id=user_id,
        session_id=session_id,
    )
    truncated = raw_result[:cap]
    if handle is None:
        summary = (
            f"{truncated}\n\n"
            f"[... {result_len - cap} more chars truncated; lossless spill "
            "was unavailable ...]"
        )
    else:
        summary = (
            f"{truncated}\n\n"
            f"[... {result_len - cap} more chars omitted from context ...]\n"
            f"[Full result handle: {handle} — use read_tool_result with this "
            "exact handle and bounded offset/limit or literal pattern]"
        )

    if len(summary) > cap + 2000:
        summary = summary[:cap + 2000]
    return summary, handle


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
        sandbox, and the message includes an opaque handle for the bounded
        ``read_tool_result`` capability.
    """
    wrapped, _handle = wrap_result_with_receipt(
        raw_result,
        tool_name,
        user_id=user_id,
        session_id=session_id,
    )
    return wrapped


def persist_result_for_history(
    raw_result: str,
    tool_name: str,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Persist a result whose executable arguments will be removed from history."""
    path = _persist_path(
        tool_name,
        raw_result,
        user_id,
        session_id,
        require_lossless=False,
    )
    return path or "[Result too large to persist]"


def get_tool_caps() -> dict[str, int]:
    """Return a copy of the per-tool cap configuration (for debugging)."""
    return dict(_TOOL_CAPS)
