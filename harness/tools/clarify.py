"""Clarify tool — ask the user a question during a conversation.

Returns the question as a structured response. The LLM should present the
question to the user in its output. The user's next message provides the answer.
"""

from __future__ import annotations

import json
from typing import Any

# Per-session store for pending clarify questions
_pending: dict[str, dict[str, Any]] = {}


async def clarify(
    question: str,
    choices: list[str] | None = None,
    session_id: str = "default",
) -> str:
    """Ask the user a clarifying question.

    Args:
        question: The question to ask the user.
        choices: Optional list of predefined choices (max 4).
        session_id: Session identifier.

    Returns:
        JSON with the question and choices presented to the user.
    """
    if not question or not question.strip():
        return json.dumps({"error": "Question must not be empty."})

    if choices is not None:
        if not isinstance(choices, list):
            return json.dumps({"error": "choices must be a list of strings."})
        choices = [str(c).strip() for c in choices[:4] if str(c).strip()]

    result = {
        "status": "asked",
        "question": question.strip(),
    }
    if choices:
        result["choices"] = choices

    _pending[session_id] = result
    return json.dumps(result, ensure_ascii=False)


CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a clarifying question when you need more information "
        "to complete the task. Present the question clearly in your response "
        "and wait for the user's answer before proceeding.\n\n"
        "Use this when:\n"
        "- Multiple valid approaches exist and you need the user to choose\n"
        "- Requirements are ambiguous and need clarification\n"
        "- You need confirmation before a destructive or risky action\n\n"
        "Limit to at most 4 choices. Avoid asking when the answer is obvious "
        "from context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user. Be clear and specific.",
            },
            "choices": {
                "type": "array",
                "description": "Optional predefined choices for the user (max 4).",
                "items": {"type": "string"},
                "maxItems": 4,
            },
        },
        "required": ["question"],
    },
}