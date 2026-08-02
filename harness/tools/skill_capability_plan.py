"""Typed selection tool for a runtime-owned standard-Skill capability catalog."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from skill_capability_plan import validate_capability_plan
from tools.context import ToolContext
from workflow_ir import (
    InstructionDocument,
    workflow_ir_json_schema,
    workflow_plan_json_schema,
)


def revalidate_capability_plan_live_authority(
    skill_name: str,
    catalog: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any] | None:
    """Revalidate every live file bound by one frozen capability catalog.

    The planning handler and the outer atomic installer call this same helper.
    The second check closes the handler-to-install TOCTOU window: no grant is
    committed from a catalog whose main document, disclosed authority, or
    runtime instruction document changed after the handler returned.
    """

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
        current_digest = hashlib.sha256(current_main.read_bytes()).hexdigest()
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "status": "error",
            "error_code": "capability_plan_revalidation_failed",
            "error": f"Cannot revalidate the canonical SKILL.md: {exc}",
        }
    if current_digest != str(catalog.get("body_sha256") or ""):
        return {
            "status": "error",
            "error_code": "capability_plan_document_changed",
            "error": (
                "The canonical SKILL.md changed after disclosure; read every "
                "page again before submitting a new capability plan."
            ),
            "expected_body_sha256": catalog.get("body_sha256"),
            "current_body_sha256": current_digest,
        }

    authority_documents = catalog.get("authority_documents") or []
    if authority_documents:
        try:
            from skills.path_safety import validate_skill_resource

            package_root = current_main.parent.resolve(strict=True)
            for document in authority_documents:
                if not isinstance(document, dict):
                    raise ValueError("malformed catalog authority document")
                resource_path = document.get("resource_path")
                expected_digest = document.get("sha256")
                checked = validate_skill_resource(
                    package_root,
                    resource_path,
                    expected_kind="file",
                    require_relative=True,
                )
                if not checked.valid or checked.path is None:
                    raise ValueError(
                        f"authority resource is unavailable: {resource_path}"
                    )
                actual_digest = hashlib.sha256(
                    checked.path.read_bytes()
                ).hexdigest()
                if actual_digest != expected_digest:
                    raise ValueError(
                        f"authority resource changed: {resource_path}"
                    )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "status": "error",
                "error_code": "capability_plan_authority_changed",
                "error": (
                    "A content-addressed reference changed after disclosure; "
                    f"read it again before amending the plan: {exc}"
                ),
            }

    instruction_documents = catalog.get("instruction_documents")
    if instruction_documents is not None:
        try:
            from skills.path_safety import validate_skill_resource

            if (
                not isinstance(instruction_documents, (list, tuple))
                or not instruction_documents
            ):
                raise ValueError("malformed runtime-owned instruction documents")
            package_root = current_main.parent.resolve(strict=True)
            for document in instruction_documents:
                if not isinstance(document, InstructionDocument):
                    raise ValueError("malformed runtime-owned instruction document")
                if document.source_path == "SKILL.md":
                    checked_path = current_main
                else:
                    checked = validate_skill_resource(
                        package_root,
                        document.source_path,
                        expected_kind="file",
                        require_relative=True,
                    )
                    if not checked.valid or checked.path is None:
                        raise ValueError(
                            "instruction resource is unavailable: "
                            + document.source_path
                        )
                    checked_path = checked.path
                actual_digest = hashlib.sha256(
                    checked_path.read_bytes()
                ).hexdigest()
                if actual_digest != document.source_sha256:
                    raise ValueError(
                        "instruction resource changed: " + document.source_path
                    )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "status": "error",
                "error_code": "capability_plan_instruction_document_changed",
                "error": (
                    "A runtime-owned Workflow IR instruction document "
                    "changed after disclosure; read it again before "
                    f"submitting a plan: {exc}"
                ),
            }
    return None


async def submit_skill_capability_plan(
    skill_name: str,
    body_sha256: str,
    required: list[str],
    optional: list[str],
    unsupported: list[dict[str, str]],
    catalog_sha256: str | None = None,
    workflow_ir: dict[str, Any] | None = None,
    workflow_plan: dict[str, Any] | None = None,
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
            catalog_sha256=catalog_sha256,
            workflow_ir=workflow_ir,
            workflow_plan=workflow_plan,
        )
        return json.dumps(result.payload, ensure_ascii=False)
    authority_failure = revalidate_capability_plan_live_authority(
        skill_name,
        catalog,
        context,
    )
    if authority_failure is not None:
        return json.dumps(authority_failure, ensure_ascii=False)
    result = validate_capability_plan(
        catalog,
        skill_name=skill_name,
        body_sha256=body_sha256,
        required=required,
        optional=optional,
        unsupported=unsupported,
        catalog_sha256=catalog_sha256,
        workflow_ir=workflow_ir,
        workflow_plan=workflow_plan,
    )
    return json.dumps(result.payload, ensure_ascii=False)


SUBMIT_SKILL_CAPABILITY_PLAN_SCHEMA: dict[str, Any] = {
    "name": "submit_skill_capability_plan",
    "description": (
        "After reading every page of the selected standard Skill's canonical "
        "SKILL.md, classify only backend-issued capability IDs as required or "
        "optional and record instructions unsupported by the finite catalog. "
        "If the Harness later exposes a content-addressed catalog amendment, "
        "submit one replacement plan with its exact catalog_sha256. This tool "
        "cannot create grants. When the catalog says workflow_ir_required, "
        "submit the compact workflow_plan against its exact instruction "
        "index; the runtime compiles the complete Workflow IR. Legacy direct "
        "callers may still submit workflow_ir, but never both."
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
            "catalog_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": (
                    "Required for a non-zero catalog revision; copy the exact "
                    "content-addressed catalog digest."
                ),
            },
            "required": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                "maxItems": 256,
            },
            "optional": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                "maxItems": 32,
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
            "workflow_ir": workflow_ir_json_schema(),
            "workflow_plan": workflow_plan_json_schema(),
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
