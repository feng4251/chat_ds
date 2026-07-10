"""Shared guards for compacted conversation-history placeholders."""

from __future__ import annotations

import re
from typing import Any

_OMITTED_HISTORY_RE = re.compile(
    r"(?:"
    r"\[omitted\s+\d+\s+chars\s+from conversation history"
    r"(?:\s+for [^\]]+)?;\s+use the workspace file or tool result if needed\]"
    r"|__CHATDS_OMITTED_TOOL_CONTENT_[A-Z_]+__"
    r"|__CHATDS_OMITTED_TOOL_ARGUMENT_\d+_CHARS__"
    r"|\[large argument omitted:\s*\d+\s*chars(?:[^\]]*)\]"
    r")",
    re.IGNORECASE,
)


def contains_compacted_history_omission(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return bool(_OMITTED_HISTORY_RE.search(value))


def compacted_history_omission_error(field: str) -> dict:
    return {
        "error": (
            f"{field} contains a compacted conversation-history placeholder, not real file content. "
            "Regenerate the actual content or read the workspace/source file before retrying."
        ),
        "reason": "invalid_placeholder_content",
    }
