"""Bounded readback for exact runtime-owned oversized tool-result handles."""

from __future__ import annotations

import json
import os
import stat
from typing import Any

from tools.context import ToolContext
from tools.execution_fence import require_execution_authority
from tools.path_security import sandbox_dir
from tools.tool_result_storage import (
    _MAX_PERSIST_BYTES,
    result_path_for_handle,
)
from tools.workspace_lock import workspace_mutation_guard


MAX_READ_CHARS = 20_000
MAX_PATTERN_CHARS = 1_024


def _error(code: str, message: str) -> str:
    return json.dumps(
        {"status": "error", "error_code": code, "error": message},
        ensure_ascii=False,
    )


async def read_tool_result(
    handle: str,
    offset: int = 0,
    limit: int = MAX_READ_CHARS,
    from_end: bool = False,
    pattern: str | None = None,
    *,
    context: ToolContext | None = None,
) -> str:
    """Read one bounded character window from an exact granted spill handle."""

    require_execution_authority(
        context,
        boundary="read_tool_result.entry",
    )
    if context is None or handle not in set(
        context.allowed_tool_result_handles
    ):
        return _error(
            "tool_result_handle_not_granted",
            "The handle was not created and granted by this runtime run.",
        )
    relative = result_path_for_handle(handle)
    if relative is None:
        return _error(
            "invalid_tool_result_handle",
            "The tool-result handle is malformed.",
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _error("invalid_offset", "offset must be a non-negative integer.")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_READ_CHARS
    ):
        return _error(
            "invalid_limit",
            f"limit must be between 1 and {MAX_READ_CHARS} characters.",
        )
    if pattern is not None and (
        not isinstance(pattern, str)
        or not pattern
        or len(pattern) > MAX_PATTERN_CHARS
    ):
        return _error(
            "invalid_pattern",
            f"pattern must be 1 to {MAX_PATTERN_CHARS} literal characters.",
        )

    workspace = sandbox_dir(
        context.user_id,
        context.session_id,
        sub="workspace",
    )
    filename = relative.removeprefix("results/")
    with workspace_mutation_guard(workspace):
        results_dir = sandbox_dir(
            context.user_id,
            context.session_id,
            sub="results",
        )
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory_fd = os.open(results_dir, directory_flags)
        try:
            try:
                descriptor = os.open(filename, flags, dir_fd=directory_fd)
            except OSError:
                return _error(
                    "tool_result_unavailable",
                    "The granted tool result is unavailable or unsafe.",
                )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1
                    or metadata.st_size > _MAX_PERSIST_BYTES
                ):
                    return _error(
                        "tool_result_unavailable",
                        "The granted tool result is not a bounded regular file.",
                    )
                with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                    descriptor = -1
                    value = stream.read(_MAX_PERSIST_BYTES + 1)
            except (OSError, UnicodeError):
                return _error(
                    "tool_result_unavailable",
                    "The granted tool result could not be read as UTF-8 text.",
                )
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        finally:
            os.close(directory_fd)

    total = len(value)
    match_offset: int | None = None
    if pattern is not None:
        if from_end:
            search_end = max(0, total - offset)
            match_offset = value.rfind(pattern, 0, search_end)
        else:
            match_offset = value.find(pattern, min(offset, total))
        if match_offset < 0:
            return _error(
                "tool_result_pattern_not_found",
                "The literal pattern was not found in the granted result.",
            )
        start = match_offset
    elif from_end:
        start = max(0, total - offset - limit)
    else:
        start = min(offset, total)
    end = min(total, start + limit)
    payload: dict[str, Any] = {
        "status": "success",
        "handle": handle,
        "content": value[start:end],
        "start_offset": start,
        "end_offset": end,
        "total_chars": total,
        "has_more_before": start > 0,
        "has_more_after": end < total,
    }
    if match_offset is not None:
        payload["match_offset"] = match_offset
    return json.dumps(payload, ensure_ascii=False)


READ_TOOL_RESULT_SCHEMA = {
    "name": "read_tool_result",
    "description": (
        "Read a bounded character slice from an exact oversized tool-result "
        "handle issued by this run. This is not a workspace file reader."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_READ_CHARS,
                "default": MAX_READ_CHARS,
            },
            "from_end": {"type": "boolean", "default": False},
            "pattern": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": MAX_PATTERN_CHARS,
            },
        },
        "required": ["handle"],
        "additionalProperties": False,
    },
}
