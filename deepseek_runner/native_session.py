"""Pure browser-transcript to native DeepSeek Session input binding."""

from __future__ import annotations

import re
from typing import Any


class NativeSessionInputError(ValueError):
    """The current browser Turn cannot be bound to one native Session input."""


SAFE_SKILL_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _message_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(content or "")


def native_turn_prompts(
    messages: list[dict[str, Any]],
    user_turn_text: str,
) -> tuple[str, str]:
    """Return a one-time history import and this exact user Turn."""

    transcript: list[tuple[str, str]] = []
    current_user_text: str | None = None
    for message in messages:
        role = str(message.get("role") or "user").lower()
        if role == "system" or role not in {"user", "assistant"}:
            continue
        text = _message_text(message.get("content"))
        transcript.append((role, text))
        if role == "user":
            current_user_text = text
    requested_text = user_turn_text if user_turn_text.strip() else current_user_text
    if (
        current_user_text is None
        or requested_text is None
        or not requested_text.strip()
        or current_user_text != requested_text
    ):
        raise NativeSessionInputError(
            "DeepSeek Harness Turn input is not bound to the current user message"
        )
    if len(transcript) == 1 and transcript[0][0] == "user":
        initial_prompt = transcript[0][1]
    else:
        initial_prompt = "\n\n".join(
            f"<{role.upper()}>\n{text}\n</{role.upper()}>"
            for role, text in transcript
        )
    if not initial_prompt.strip():
        raise NativeSessionInputError(
            "DeepSeek Harness initial Session input is empty"
        )
    return initial_prompt, requested_text


def bind_native_primary_skill(
    initial_prompt: str,
    skill_name: str | None,
) -> str:
    """Lower one selected Skill to DSH's native fresh-Session gesture.

    The native Session driver chooses ``initial_prompt`` only when no durable
    Session state exists, so this binding is neither a control prompt nor a
    replayed resume instruction.  DSH remains responsible for loading and
    executing the Skill through its own tool graph.
    """

    if skill_name is None:
        return initial_prompt
    if SAFE_SKILL_NAME.fullmatch(skill_name) is None:
        raise NativeSessionInputError("DeepSeek Harness Skill identity is invalid")
    invocation = f"/{skill_name}"
    if re.search(rf"(?<!\S){re.escape(invocation)}(?!\S)", initial_prompt):
        return initial_prompt
    return f"{invocation}\n{initial_prompt}"
