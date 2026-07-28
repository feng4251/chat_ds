"""Typed control-plane decision tool for compiled worker knowledge gates."""

from __future__ import annotations

import json
from typing import Any

from knowledge_gate_runtime import (
    KNOWLEDGE_GATE_DECISION_TOOL_NAME,
    validate_knowledge_gate_decisions,
)
from tools.context import ToolContext


async def submit_knowledge_gate_decisions(
    plan_sha256: str,
    decisions: list[dict[str, Any]],
    context: ToolContext | None = None,
) -> str:
    """Activate only branches from the current runtime-owned gate plan."""

    plan = context.knowledge_gate_plan if context is not None else None
    expected = (
        context.knowledge_gate_plan_sha256
        if context is not None
        else ""
    )
    result = validate_knowledge_gate_decisions(
        plan,
        expected_sha256=expected,
        supplied_sha256=plan_sha256,
        decisions=decisions,
    )
    return json.dumps(result, ensure_ascii=False)


SUBMIT_KNOWLEDGE_GATE_DECISIONS_SCHEMA: dict[str, Any] = {
    "name": KNOWLEDGE_GATE_DECISION_TOOL_NAME,
    "description": (
        "Submit one typed decision for every check in the active "
        "Harness-compiled worker knowledge-gate plan. The call activates only "
        "backend-issued conditional candidate groups and grants no capability."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "plan_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "decisions": {
                "type": "array",
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "properties": {
                        "check_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                        },
                        "outcome": {
                            "type": "string",
                            "enum": [
                                "yes", "no", "unknown",
                            ],
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1_000,
                        },
                    },
                    "required": ["check_id", "outcome", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["plan_sha256", "decisions"],
        "additionalProperties": False,
    },
}
