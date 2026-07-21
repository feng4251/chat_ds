"""Typed selection tool for a runtime-owned standard-Skill capability catalog."""

from __future__ import annotations

import json
from typing import Any

from skill_capability_plan import validate_capability_plan
from tools.context import ToolContext


async def submit_skill_capability_plan(
    skill_name: str,
    body_sha256: str,
    required: list[str],
    optional: list[str],
    unsupported: list[dict[str, str]],
    context: ToolContext | None = None,
) -> str:
    """Select only capabilities issued in the current runtime catalog."""

    catalog = (
        context.skill_capability_catalog
        if context is not None else None
    )
    if context is None or not isinstance(catalog, dict):
        result = validate_capability_plan(
            catalog,
            skill_name=skill_name,
            body_sha256=body_sha256,
            required=required,
            optional=optional,
            unsupported=unsupported,
        )
        return json.dumps(result.payload, ensure_ascii=False)
    try:
        from skills.scanner import resolve_skill_path

        current_main = resolve_skill_path(
            skill_name,
            context.user_id,
            context.session_id,
            enabled_user_skills=list(context.enabled_user_skills),
        )
        if current_main is None:
            raise ValueError("the selected Skill is no longer installed/enabled")
        current_digest = __import__("hashlib").sha256(
            current_main.read_bytes()
        ).hexdigest()
    except (OSError, RuntimeError, ValueError) as exc:
        return json.dumps({
            "status": "error",
            "error_code": "capability_plan_revalidation_failed",
            "error": f"Cannot revalidate the canonical SKILL.md: {exc}",
        }, ensure_ascii=False)
    if current_digest != str(catalog.get("body_sha256") or ""):
        return json.dumps({
            "status": "error",
            "error_code": "capability_plan_document_changed",
            "error": (
                "The canonical SKILL.md changed after disclosure; read every "
                "page again before submitting a new capability plan."
            ),
            "expected_body_sha256": catalog.get("body_sha256"),
            "current_body_sha256": current_digest,
        }, ensure_ascii=False)
    result = validate_capability_plan(
        catalog,
        skill_name=skill_name,
        body_sha256=body_sha256,
        required=required,
        optional=optional,
        unsupported=unsupported,
    )
    return json.dumps(result.payload, ensure_ascii=False)


SUBMIT_SKILL_CAPABILITY_PLAN_SCHEMA: dict[str, Any] = {
    "name": "submit_skill_capability_plan",
    "description": (
        "After reading every page of the selected standard Skill's canonical "
        "SKILL.md, classify only backend-issued capability IDs as required or "
        "optional and record instructions unsupported by the finite catalog. "
        "This tool cannot create grants."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
            },
            "body_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "required": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                "maxItems": 256,
            },
            "optional": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                "maxItems": 256,
            },
            "unsupported": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1000,
                        },
                    },
                    "required": ["instruction", "reason"],
                    "additionalProperties": False,
                },
                "maxItems": 64,
            },
        },
        "required": [
            "skill_name",
            "body_sha256",
            "required",
            "optional",
            "unsupported",
        ],
        "additionalProperties": False,
    },
}
