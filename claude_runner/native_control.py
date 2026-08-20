"""Typed Web I/O lowering for Claude Code native control requests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


NATIVE_USER_INTERACTION_TOOLS = frozenset({
    "AskUserQuestion",
    "ExitPlanMode",
    "ReviewArtifact",
})
MAX_NATIVE_CONTROL_RESPONSE_BYTES = 16 * 1024 * 1024


def validate_native_question_input(native_input: object) -> tuple[str, ...]:
    """Validate the browser-answerable subset of Claude's native schema.

    The native input remains authoritative and is never reconstructed from
    browser display data. This validation only ensures that the controller
    and browser projector agree on whether a waiter can be answered safely.
    """

    if not isinstance(native_input, Mapping):
        raise ValueError("native_question_input_invalid")
    questions = native_input.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        raise ValueError("native_question_input_invalid")
    expected: list[str] = []
    for row in questions:
        if not isinstance(row, Mapping):
            raise ValueError("native_question_input_invalid")
        question = row.get("question")
        header = row.get("header")
        options = row.get("options")
        multi_select = row.get("multiSelect", False)
        if (
            not isinstance(question, str)
            or not question
            or len(question) > 4_000
            or question in expected
            or not isinstance(header, str)
            or len(header) > 256
            or not isinstance(options, list)
            or not 2 <= len(options) <= 4
            or not isinstance(multi_select, bool)
        ):
            raise ValueError("native_question_input_invalid")
        labels: set[str] = set()
        for option in options:
            if not isinstance(option, Mapping):
                raise ValueError("native_question_input_invalid")
            label = option.get("label")
            description = option.get("description")
            if (
                not isinstance(label, str)
                or not label
                or len(label) > 256
                or label in labels
                or not isinstance(description, str)
                or len(description) > 2_000
            ):
                raise ValueError("native_question_input_invalid")
            labels.add(label)
        expected.append(question)
    return tuple(expected)


def native_user_interaction_kind(
    tool_name: object,
    native_input: object,
) -> str | None:
    """Classify only native requests that the Web I/O boundary can settle."""

    name = str(tool_name or "")
    if name == "AskUserQuestion":
        try:
            validate_native_question_input(native_input)
        except ValueError:
            return None
        return "question"
    if name in {"ExitPlanMode", "ReviewArtifact"}:
        return "user_action"
    return None


def is_native_user_interaction(tool_name: object) -> bool:
    """Return whether a tool is intrinsically interactive by native name."""

    return str(tool_name or "") in NATIVE_USER_INTERACTION_TOOLS


def build_native_updated_input(
    *,
    tool_name: object,
    native_input: object,
    answers: object,
) -> dict[str, Any] | None:
    """Bind browser answers to the exact durable native question input."""

    name = str(tool_name or "")
    if name != "AskUserQuestion":
        if answers is not None:
            raise ValueError("native_control_answers_unexpected")
        return None
    expected = validate_native_question_input(native_input)
    if (
        not isinstance(answers, Mapping)
        or set(answers) != set(expected)
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not value.strip()
            or len(value) > 4_000
            or "\x00" in value
            for key, value in answers.items()
        )
    ):
        raise ValueError("native_question_answers_invalid")
    updated = {
        **dict(native_input),
        "answers": {question: str(answers[question]) for question in expected},
    }
    encoded = json.dumps(
        updated,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_NATIVE_CONTROL_RESPONSE_BYTES:
        raise ValueError("native_question_response_too_large")
    return updated
