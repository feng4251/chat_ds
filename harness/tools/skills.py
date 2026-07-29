"""Skills tools — progressive disclosure skill listing and viewing.

Two tools:
  - skills_list: List available skills (name + description only, token-efficient)
  - skill_view: Load full skill content or access linked files

Simplified from hermes-agent/tools/skills_tool.py:
- No plugin/namespace system
- No platform matching
- No disabled-skills filtering
- No secret capture / env var requirements
- No telemetry (bump_use/bump_view)
- No credential file registration
"""

from __future__ import annotations

import codecs
import hashlib
import json
import logging
import mimetypes
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from skills.manager import (
    DEFAULT_SKILL_MANIFEST_PAGE_ENTRIES,
    MAX_SKILL_MANIFEST_OFFSET_ENTRIES,
    MAX_SKILL_MANIFEST_PAGE_ENTRIES,
    get_manager,
)
from skills.path_safety import validate_skill_resource
from skills.scanner import find_all_skills
from tools.context import ToolContext
from tools.execution_fence import require_execution_authority
from tools.path_security import validate_path
from tools.workspace_lock import (
    run_sync_cancellation_safe,
    workspace_mutation_guard,
)

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_CANDIDATE_FILES = 12
CANDIDATE_FILE_KEYWORDS = ("reference", "ground", "truth", "template", "format", "example", "evaluation", "script")
DEFAULT_SKILL_VIEW_PAGE_CHARS = 40_000
MAX_SKILL_VIEW_PAGE_CHARS = 100_000
MAX_SKILL_VIEW_OFFSET_CHARS = 25_600_000
_SKILL_RESOURCE_READ_CHUNK_BYTES = 64 * 1024
_MAX_SKILL_VIEW_SERIALIZED_CONTENT_CHARS = 40_000
# ``tool_result_storage`` applies a 50,000-character generic result cap.  Keep
# the tier-2 Skill activation envelope below that boundary so it remains one
# parseable JSON document instead of a JSON prefix followed by a truncation
# notice.  The canonical resource inventory remains available through the
# paginated ``__manifest__`` view.
_MAX_SKILL_ACTIVATION_RESULT_CHARS = 48_000
_MAX_ACTIVATION_RESOURCE_CATEGORIES = 32
_MAX_ACTIVATION_RESOURCES_PER_CATEGORY = 3
_MAX_ACTIVATION_RESOURCE_ENTRIES = 48
_MAX_ACTIVATION_RESOURCE_PATH_BYTES = 1_024
_MAX_ACTIVATION_RESOURCE_PATH_BYTES_TOTAL = 8_192
_MAX_ACTIVATION_MCP_SCRIPT_HINTS = 8
_MAX_ACTIVATION_SUMMARY_FIELDS = 24
_MAX_ACTIVATION_DIAGNOSTIC_CODES = 12
_MAX_ACTIVATION_SEQUENCE_SAMPLE = 12

# Pattern to extract default env var values from SKILL.md or script content.
# Matches blocks like:
#   PATHOLOGY_API_URL      Remote service base URL.
#                          Default: http://127.0.0.1:18018
_ENV_DEFAULT_RE = re.compile(
    r"^(\w+)\s+.*?Default:\s*(\S+)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _extract_env_hints(content: str) -> dict[str, str]:
    """Extract environment variable defaults from SKILL.md or script content."""
    hints: dict[str, str] = {}
    for match in _ENV_DEFAULT_RE.finditer(content):
        var_name = match.group(1)
        default_val = match.group(2)
        # Only capture vars that look like API URLs / config values
        if "URL" in var_name.upper() or "API" in var_name.upper():
            hints[var_name] = default_val
    return hints


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _bounded_activation_resource_indexes(
    result: dict[str, Any],
) -> dict[str, Any]:
    """Bound tier-2 resource indexes without clipping Skill instructions.

    ``load_skill_content`` deliberately retains the complete resource closure
    for compilation, authority checks, and manifest pagination.  Returning
    that same closure from the first ``skill_view(name)`` call defeats
    progressive disclosure for asset-heavy Skills: the generic tool-result
    wrapper truncates the JSON document at 50k characters.  Keep small Skill
    responses byte-for-byte compatible, but replace an oversized inventory
    with deterministic exact-path samples and accurate omission counts.

    A path whose UTF-8 representation is itself unusually large is omitted
    from the activation sample rather than returning a misleading truncated
    path.  Its exact value remains available in the canonical manifest.
    """
    linked = result.get("linked_files")
    if not isinstance(linked, dict) or not linked:
        return result

    inventory: list[tuple[str, list[str]]] = []
    has_oversized_path = False
    for raw_category, raw_paths in linked.items():
        if not isinstance(raw_category, str) or not isinstance(raw_paths, list):
            continue
        paths = [path for path in raw_paths if isinstance(path, str)]
        paths.sort(key=lambda path: (path.casefold(), path))
        if any(_utf8_size(path) > _MAX_ACTIVATION_RESOURCE_PATH_BYTES for path in paths):
            has_oversized_path = True
        inventory.append((raw_category, paths))
    inventory.sort(key=lambda item: (item[0].casefold(), item[0]))

    if not has_oversized_path:
        serialized_chars = len(json.dumps(result, ensure_ascii=False))
        if serialized_chars <= _MAX_SKILL_ACTIVATION_RESULT_CHARS:
            return result

    total_entries = sum(len(paths) for _category, paths in inventory)
    total_categories = len(inventory)
    sampled_files: dict[str, list[str]] = {}
    category_summary: dict[str, dict[str, int]] = {}
    returned_entries = 0
    used_path_bytes = 0
    oversized_paths = 0
    returned_categories = 0

    for category, paths in inventory:
        if returned_categories >= _MAX_ACTIVATION_RESOURCE_CATEGORIES:
            oversized_paths += sum(
                1
                for path in paths
                if _utf8_size(path) > _MAX_ACTIVATION_RESOURCE_PATH_BYTES
            )
            continue

        sample: list[str] = []
        category_oversized = 0
        for path in paths:
            path_bytes = _utf8_size(path)
            if path_bytes > _MAX_ACTIVATION_RESOURCE_PATH_BYTES:
                category_oversized += 1
                continue
            if len(sample) >= _MAX_ACTIVATION_RESOURCES_PER_CATEGORY:
                continue
            if returned_entries >= _MAX_ACTIVATION_RESOURCE_ENTRIES:
                continue
            if used_path_bytes + path_bytes > _MAX_ACTIVATION_RESOURCE_PATH_BYTES_TOTAL:
                continue
            sample.append(path)
            returned_entries += 1
            used_path_bytes += path_bytes

        oversized_paths += category_oversized
        returned_categories += 1
        if sample:
            sampled_files[category] = sample
        category_summary[category] = {
            "total": len(paths),
            "returned": len(sample),
            "omitted": len(paths) - len(sample),
            "oversized_paths_omitted": category_oversized,
        }

    omitted_entries = total_entries - returned_entries
    omitted_categories = total_categories - returned_categories
    original_graph = result.get("resource_graph")
    graph_root = (
        original_graph.get("skill_root")
        if isinstance(original_graph, dict)
        else None
    )
    compact_graph: dict[str, Any] = {
        "categories": {
            category: {
                "count": summary["total"],
                "sample": sampled_files.get(category, []),
                "sample_truncated": summary["omitted"] > 0,
                "sample_omitted": summary["omitted"],
            }
            for category, summary in category_summary.items()
        },
        "important_categories": list(category_summary),
        "suggested_files": [
            path
            for category in category_summary
            for path in sampled_files.get(category, [])
        ],
        "suggested_files_truncated": omitted_entries > 0,
        "inventory_truncated": True,
        "hint": (
            "This activation response contains deterministic resource samples. "
            "Use skill_view(name, file_path='__manifest__') and its exact "
            "next_offset values for the complete canonical inventory."
        ),
    }
    if isinstance(graph_root, str):
        compact_graph["skill_root"] = graph_root

    bounded = dict(result)
    bounded["linked_files"] = sampled_files
    bounded["linked_files_truncated"] = omitted_entries > 0
    bounded["linked_file_count"] = total_entries
    bounded["linked_files_returned_count"] = returned_entries
    bounded["linked_files_omitted_count"] = omitted_entries
    bounded["linked_file_category_count"] = total_categories
    bounded["linked_file_categories_returned_count"] = returned_categories
    bounded["linked_file_categories_omitted_count"] = omitted_categories
    bounded["linked_file_oversized_paths_omitted_count"] = oversized_paths
    bounded["linked_files_summary"] = category_summary
    bounded["linked_files_manifest"] = {
        "file_path": "__manifest__",
        "offset": 0,
        "default_limit": DEFAULT_SKILL_MANIFEST_PAGE_ENTRIES,
        "maximum_limit": MAX_SKILL_MANIFEST_PAGE_ENTRIES,
        "pagination": "Follow only the exact next_offset returned by each page.",
    }
    bounded["resource_graph"] = compact_graph
    bounded["usage_hint"] = (
        "The complete Skill instructions are present in this activation response. "
        "The resource index is sampled; call skill_view(name, "
        "file_path='__manifest__') for its stable paginated inventory, then "
        "load only request-relevant files."
    )
    return bounded


def _canonical_json_value(value: Any) -> Any:
    """Return an order-independent JSON value for envelope integrity hashes."""
    if isinstance(value, dict):
        items = [
            [
                [type(key).__name__, str(key)],
                _canonical_json_value(child),
            ]
            for key, child in value.items()
        ]
        items.sort(
            key=lambda item: json.dumps(
                item[0], ensure_ascii=False, separators=(",", ":")
            )
        )
        return {"__mapping__": items}
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, tuple):
        return {"__tuple__": [_canonical_json_value(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        items = [_canonical_json_value(item) for item in value]
        items.sort(
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
        return {"__set__": items}
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"__type__": type(value).__name__, "value": str(value)}


def _stable_json_text(value: Any) -> str:
    """Serialize compiler-owned data canonically for envelope hashes."""
    return json.dumps(
        _canonical_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_value_summary(value: Any, *, kind: str) -> dict[str, Any]:
    serialized = _stable_json_text(value)
    summary: dict[str, Any] = {
        "kind": kind,
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "serialized_chars": len(serialized),
    }
    if isinstance(value, dict):
        fields = sorted(
            (str(key) for key in value),
            key=lambda field: (field.casefold(), field),
        )
        summary["field_count"] = len(fields)
        summary["fields"] = fields[:_MAX_ACTIVATION_SUMMARY_FIELDS]
        summary["fields_omitted"] = max(
            0, len(fields) - _MAX_ACTIVATION_SUMMARY_FIELDS
        )
    elif isinstance(value, (list, tuple)):
        summary["item_count"] = len(value)
    return summary


def _collection_count(value: Any) -> int:
    return len(value) if isinstance(value, (dict, list, tuple)) else 0


def _safe_activation_scalar(value: Any, *, max_bytes: int = 2_048) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if not isinstance(value, str):
        return None
    if _utf8_size(value) <= max_bytes:
        return value
    encoded = value.encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "utf8_bytes": len(encoded),
        "omitted_from_activation_envelope": True,
    }


def _diagnostics_summary(value: Any) -> dict[str, Any]:
    summary = _stable_value_summary(value, kind="package_diagnostics")
    if not isinstance(value, dict):
        return summary
    counts: dict[str, int] = {}
    codes: list[str] = []
    for level in ("errors", "warnings", "info"):
        items = value.get(level)
        items = items if isinstance(items, list) else []
        counts[level] = len(items)
        for item in items:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if isinstance(code, str) and code not in codes:
                codes.append(code)
    summary["valid"] = value.get("valid")
    summary["counts"] = counts
    summary["code_sample"] = codes[:_MAX_ACTIVATION_DIAGNOSTIC_CODES]
    summary["codes_omitted"] = max(
        0, len(codes) - _MAX_ACTIVATION_DIAGNOSTIC_CODES
    )
    return summary


def _output_contract_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    summary: dict[str, Any] = {}
    for field in (
        "declared_file_count",
        "declared_modular_file_count",
        "merge_mandatory",
        "artifact_set_mode",
    ):
        if field in value:
            scalar = _safe_activation_scalar(value.get(field))
            if scalar is not None:
                summary[field] = scalar
    final_artifact = _safe_activation_scalar(
        value.get("declared_final_artifact"),
        max_bytes=_MAX_ACTIVATION_RESOURCE_PATH_BYTES,
    )
    if final_artifact is not None:
        summary["declared_final_artifact"] = final_artifact
    counts = {
        field: _collection_count(value.get(field))
        for field in (
            "declared_artifacts",
            "declared_modular_files",
            "declared_ancillary_files",
            "merge_input_order",
            "sections",
            "post_merge_checks",
        )
        if _collection_count(value.get(field))
    }
    if counts:
        summary["counts"] = counts
    return summary


def _contract_summary(value: Any, *, kind: str) -> dict[str, Any]:
    summary = _stable_value_summary(value, kind=kind)
    if not isinstance(value, dict):
        return summary

    counts: dict[str, int] = {}
    for field in ("workers", "routes", "routing_rules", "tasks", "steps", "sources"):
        count = _collection_count(value.get(field))
        if count:
            counts[field] = count
    bootstrap = value.get("knowledge_bootstrap")
    if isinstance(bootstrap, dict):
        count = _collection_count(bootstrap.get("sources"))
        if count:
            counts["knowledge_bootstrap.sources"] = count
    aggregation = value.get("aggregation")
    if isinstance(aggregation, dict):
        count = _collection_count(aggregation.get("steps"))
        if count:
            counts["aggregation.steps"] = count
    intent = value.get("intent_classification")
    if isinstance(intent, dict):
        count = _collection_count(intent.get("dimensions"))
        if count:
            counts["intent_classification.dimensions"] = count
    if counts:
        summary["counts"] = counts

    output = _output_contract_summary(value.get("output_contract"))
    if output:
        summary["output_contract"] = output
    diagnostics = value.get("diagnostics")
    if isinstance(diagnostics, dict):
        summary["diagnostics"] = {
            "valid": diagnostics.get("valid"),
            "error_count": len(diagnostics.get("errors") or []),
            "warning_count": len(diagnostics.get("warnings") or []),
            "info_count": len(diagnostics.get("info") or []),
        }
    summary["inspection"] = (
        "Use the stable paginated __manifest__ inventory to locate the exact "
        "workflow/reference source files, then read only the request-relevant files."
    )
    return summary


def _frontmatter_summary(value: Any) -> dict[str, Any]:
    summary = _stable_value_summary(value, kind="skill_frontmatter")
    if not isinstance(value, dict):
        return summary
    standard: dict[str, Any] = {}
    for field in (
        "name", "description", "license", "compatibility", "allowed-tools"
    ):
        if field not in value:
            continue
        scalar = _safe_activation_scalar(value.get(field))
        if scalar is not None:
            standard[field] = scalar
    if standard:
        summary["standard_fields"] = standard
    return summary


def _bounded_sequence_summary(value: Any, *, kind: str) -> dict[str, Any]:
    summary = _stable_value_summary(value, kind=kind)
    if isinstance(value, (list, tuple)):
        safe_sample: list[Any] = []
        for item in value:
            scalar = _safe_activation_scalar(item, max_bytes=512)
            if scalar is None:
                continue
            safe_sample.append(scalar)
            if len(safe_sample) >= _MAX_ACTIVATION_SEQUENCE_SAMPLE:
                break
        if safe_sample:
            summary["sample"] = safe_sample
        summary["items_omitted"] = max(0, len(value) - len(safe_sample))
    return summary


def _compact_large_skill_view_envelope(
    result: dict[str, Any],
    *,
    manifest_view: bool,
) -> dict[str, Any]:
    """Separate loader-owned compiler IR from the model-facing envelope."""
    if len(json.dumps(result, ensure_ascii=False)) <= _MAX_SKILL_ACTIVATION_RESULT_CHARS:
        return result

    preserved_fields = (
        "success",
        "name",
        "description",
        "content",
        "skill_md_sha256",
        "skill_md_chars",
        "path",
        "skill_dir",
        "file",
        "file_type",
        "size_bytes",
        "sha256",
        "media_type",
        "offset",
        "limit",
        "returned_chars",
        "total_chars",
        "next_offset",
        "has_more",
        "truncated",
        "pagination",
        "main_document_paged",
        "hint",
        "usage_hint",
        "linked_files",
        "linked_files_truncated",
        "linked_file_count",
        "linked_files_returned_count",
        "linked_files_omitted_count",
        "linked_file_category_count",
        "linked_file_categories_returned_count",
        "linked_file_categories_omitted_count",
        "linked_file_oversized_paths_omitted_count",
        "linked_files_summary",
        "linked_files_manifest",
        "resource_graph",
        "manifest_sha256",
        "manifest_pagination",
        "next_steps",
        "mcp_config_hint",
        "mcp_script_hint",
    )
    envelope = {
        field: result[field]
        for field in preserved_fields
        if field in result
    }
    omitted_sections: list[str] = []

    for field in ("version", "license", "compatibility"):
        if field not in result:
            continue
        scalar = _safe_activation_scalar(result.get(field))
        if scalar is not None:
            envelope[field] = scalar

    for field, summary_name, kind in (
        ("workflow_contract", "workflow_contract_summary", "workflow_contract"),
        ("execution_contract", "execution_contract_summary", "execution_contract"),
    ):
        value = result.get(field)
        if value not in (None, {}, []):
            envelope[summary_name] = _contract_summary(value, kind=kind)
            omitted_sections.append(field)

    diagnostics = result.get("package_diagnostics")
    if diagnostics not in (None, {}, []):
        envelope["package_diagnostics_summary"] = _diagnostics_summary(diagnostics)
        omitted_sections.append("package_diagnostics")

    frontmatter = result.get("frontmatter")
    if frontmatter not in (None, {}, []):
        envelope["frontmatter_summary"] = _frontmatter_summary(frontmatter)
        omitted_sections.append("frontmatter")

    mcp_servers = result.get("mcp_servers")
    if mcp_servers not in (None, {}, []):
        envelope["mcp_servers_summary"] = _stable_value_summary(
            mcp_servers,
            kind="mcp_servers",
        )
        omitted_sections.append("mcp_servers")

    for field in ("tags", "related_skills"):
        value = result.get(field)
        if value not in (None, {}, []):
            envelope[f"{field}_summary"] = _bounded_sequence_summary(
                value,
                kind=field,
            )
            omitted_sections.append(field)

    output = result.get("output_contract")
    if output not in (None, {}, []):
        envelope["output_contract_summary"] = {
            **_stable_value_summary(output, kind="output_contract"),
            "contract": _output_contract_summary(output),
        }
        omitted_sections.append("output_contract")
    execution_hint = result.get("execution_plan_hint")
    if isinstance(execution_hint, str) and execution_hint:
        envelope["execution_plan_hint_summary"] = _safe_activation_scalar(
            execution_hint,
            max_bytes=2_048,
        )
        omitted_sections.append("execution_plan_hint")

    envelope["activation_envelope_compacted"] = True
    envelope["activation_envelope_omitted_sections"] = sorted(omitted_sections)
    envelope["activation_envelope_hint"] = (
        "Compiler IR remains complete in the harness runtime. This model-facing "
        "response carries stable hashes and counts; use __manifest__ to locate "
        "and read exact declaration sources without loading unrelated resources."
    )

    if manifest_view:
        _fit_manifest_page_to_envelope(envelope)
    _fit_activation_envelope(envelope, manifest_view=manifest_view)
    return envelope


def _fit_manifest_page_to_envelope(envelope: dict[str, Any]) -> None:
    linked = envelope.get("linked_files")
    pagination = envelope.get("manifest_pagination")
    if not isinstance(linked, dict) or not isinstance(pagination, dict):
        return
    entries = sorted(
        (
            str(category),
            str(path),
        )
        for category, paths in linked.items()
        if isinstance(category, str) and isinstance(paths, list)
        for path in paths
        if isinstance(path, str)
    )
    offset = int(pagination.get("offset") or 0)
    total = int(pagination.get("total_entries") or len(entries))
    def page_for(count: int) -> dict[str, list[str]]:
        page: dict[str, list[str]] = {}
        for category, path in entries[:count]:
            page.setdefault(category, []).append(path)
        return page

    returned = len(entries)
    if len(json.dumps(envelope, ensure_ascii=False)) > _MAX_SKILL_ACTIVATION_RESULT_CHARS:
        low = 1 if entries else 0
        high = len(entries)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = dict(envelope)
            candidate["linked_files"] = page_for(midpoint)
            if len(json.dumps(candidate, ensure_ascii=False)) <= _MAX_SKILL_ACTIVATION_RESULT_CHARS:
                low = midpoint
            else:
                high = midpoint - 1
        returned = low
    page = page_for(returned)
    envelope["linked_files"] = page
    next_offset = offset + returned
    has_more = next_offset < total
    updated = dict(pagination)
    updated["returned_entries"] = returned
    updated["has_more"] = has_more
    updated["next_offset"] = next_offset if has_more else None
    updated["response_bounded"] = returned < int(
        pagination.get("returned_entries") or returned
    )
    envelope["manifest_pagination"] = updated


def _fit_activation_envelope(
    envelope: dict[str, Any],
    *,
    manifest_view: bool,
) -> None:
    """Make final overflow reductions while keeping exact instruction pages."""
    if len(json.dumps(envelope, ensure_ascii=False)) <= _MAX_SKILL_ACTIVATION_RESULT_CHARS:
        return

    # Resource samples are advisory; their exact inventory is cursor-readable.
    if not manifest_view and isinstance(envelope.get("linked_files"), dict):
        envelope["linked_files"] = {}
        total = int(envelope.get("linked_file_count") or 0)
        envelope["linked_files_returned_count"] = 0
        envelope["linked_files_omitted_count"] = total
    graph = envelope.get("resource_graph")
    if isinstance(graph, dict):
        compact_graph = {
            key: graph[key]
            for key in ("skill_root", "hint", "inventory_truncated")
            if key in graph
        }
        compact_graph["inventory_truncated"] = True
        envelope["resource_graph"] = compact_graph
    envelope.pop("linked_files_summary", None)
    envelope.pop("next_steps", None)

    if len(json.dumps(envelope, ensure_ascii=False)) <= _MAX_SKILL_ACTIVATION_RESULT_CHARS:
        return

    # Retain hashes and primary counts but drop optional field-name samples.
    for key in (
        "workflow_contract_summary",
        "execution_contract_summary",
        "package_diagnostics_summary",
        "frontmatter_summary",
        "mcp_servers_summary",
        "tags_summary",
        "related_skills_summary",
        "output_contract_summary",
    ):
        summary = envelope.get(key)
        if isinstance(summary, dict):
            for optional in (
                "fields", "standard_fields", "code_sample", "sample", "inspection"
            ):
                summary.pop(optional, None)

    if len(json.dumps(envelope, ensure_ascii=False)) <= _MAX_SKILL_ACTIVATION_RESULT_CHARS:
        return

    # A large canonical SKILL.md is already cursor-paged. Shrink only that
    # exact page and advance next_offset to the first undisclosed character.
    if envelope.get("main_document_paged") is True:
        content = envelope.get("content")
        pagination = envelope.get("pagination")
        if isinstance(content, str) and isinstance(pagination, dict):
            low = 0
            high = len(content)
            while low < high:
                midpoint = (low + high + 1) // 2
                candidate = dict(envelope)
                candidate["content"] = content[:midpoint]
                if len(json.dumps(candidate, ensure_ascii=False)) <= _MAX_SKILL_ACTIVATION_RESULT_CHARS:
                    low = midpoint
                else:
                    high = midpoint - 1
            offset = int(pagination.get("offset") or 0)
            returned = low
            total = int(pagination.get("total_chars") or len(content))
            has_more = offset + returned < total
            envelope["content"] = content[:returned]
            envelope["returned_chars"] = returned
            envelope["next_offset"] = offset + returned if has_more else None
            envelope["has_more"] = has_more
            envelope["truncated"] = has_more
            updated = dict(pagination)
            updated["returned_chars"] = returned
            updated["has_more"] = has_more
            updated["next_offset"] = offset + returned if has_more else None
            envelope["pagination"] = updated

    if manifest_view:
        _fit_manifest_page_to_envelope(envelope)

    if len(json.dumps(envelope, ensure_ascii=False)) <= _MAX_SKILL_ACTIVATION_RESULT_CHARS:
        return

    # Final deterministic fallback: retain the exact instruction content/page,
    # canonical inventory cursor, global counts, and content-addressed compiler
    # summaries.  This is intentionally a model envelope only; loader-owned IR
    # remains untouched for runtime enforcement.
    minimal_fields = (
        "success",
        "name",
        "description",
        "content",
        "skill_md_sha256",
        "skill_md_chars",
        "file",
        "sha256",
        "size_bytes",
        "offset",
        "limit",
        "returned_chars",
        "total_chars",
        "next_offset",
        "has_more",
        "truncated",
        "pagination",
        "main_document_paged",
        "linked_files",
        "linked_file_count",
        "linked_files_returned_count",
        "linked_files_omitted_count",
        "linked_file_category_count",
        "linked_file_categories_omitted_count",
        "linked_file_oversized_paths_omitted_count",
        "linked_files_manifest",
        "manifest_sha256",
        "manifest_pagination",
        "activation_envelope_compacted",
        "activation_envelope_omitted_sections",
        "activation_envelope_hint",
    )
    minimal = {
        field: envelope[field]
        for field in minimal_fields
        if field in envelope
    }
    for key in (
        "workflow_contract_summary",
        "execution_contract_summary",
        "package_diagnostics_summary",
        "frontmatter_summary",
        "mcp_servers_summary",
        "output_contract_summary",
    ):
        summary = envelope.get(key)
        if not isinstance(summary, dict):
            continue
        minimal[key] = {
            field: summary[field]
            for field in ("kind", "sha256", "serialized_chars", "counts")
            if field in summary
        }
    envelope.clear()
    envelope.update(minimal)

    if manifest_view:
        _fit_manifest_page_to_envelope(envelope)

    if (
        len(json.dumps(envelope, ensure_ascii=False))
        > _MAX_SKILL_ACTIVATION_RESULT_CHARS
        and envelope.get("main_document_paged") is True
        and isinstance(envelope.get("content"), str)
        and isinstance(envelope.get("pagination"), dict)
    ):
        content = envelope["content"]
        pagination = envelope["pagination"]
        low = 0
        high = len(content)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = dict(envelope)
            candidate["content"] = content[:midpoint]
            if len(json.dumps(candidate, ensure_ascii=False)) <= _MAX_SKILL_ACTIVATION_RESULT_CHARS:
                low = midpoint
            else:
                high = midpoint - 1
        offset = int(pagination.get("offset") or 0)
        total = int(pagination.get("total_chars") or len(content))
        has_more = offset + low < total
        envelope["content"] = content[:low]
        envelope["returned_chars"] = low
        envelope["next_offset"] = offset + low if has_more else None
        envelope["has_more"] = has_more
        envelope["truncated"] = has_more
        updated = dict(pagination)
        updated["returned_chars"] = low
        updated["has_more"] = has_more
        updated["next_offset"] = offset + low if has_more else None
        envelope["pagination"] = updated


async def skills_list(
    category: str | None = None,
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> str:
    """List all available skills (progressive disclosure tier 1 — minimal metadata).

    Returns only name + description to minimize token usage. Use skill_view()
    to load full content, tags, related files, etc.

    Args:
        category: Optional category filter (e.g., "mlops").
        user_id: User identifier for per-user skill isolation.
        session_id: Session identifier.
        enabled_user_skills: Whitelist of user-level skill names to expose.
            When provided, user-level skills not in this list are hidden.

    Returns:
        JSON string with skills list and categories.
    """
    try:
        mgr = get_manager()
        include_optional = mgr.get_session_optional(session_id)
        all_skills = find_all_skills(
            user_id,
            session_id,
            include_optional=include_optional,
            enabled_user_skills=enabled_user_skills,
        )

        if not all_skills:
            return json.dumps(
                {
                    "success": True,
                    "skills": [],
                    "categories": [],
                    "message": "No skills found.",
                },
                ensure_ascii=False,
            )

        # Filter by category if specified
        if category:
            all_skills = [s for s in all_skills if s.get("category") == category]

        # Extract unique categories
        categories = sorted(
            {s.get("category") for s in all_skills if s.get("category")}
        )

        # Strip internal fields from output
        output_skills = [
            {
                "name": s["name"],
                "description": s["description"],
                "category": s.get("category"),
                "scope": s.get("scope"),
            }
            for s in all_skills
        ]

        return json.dumps(
            {
                "success": True,
                "skills": output_skills,
                "categories": categories,
                "count": len(output_skills),
                "hint": (
                    "Use skill_view(name) to see full content. For complex skills, then use "
                    "skill_view(name, file_path='__manifest__') to inspect workflow resources, "
                    "especially report templates, reference/ground-truth examples, evaluation files, "
                    "and scripts before drafting final Markdown."
                ),
            },
            ensure_ascii=False,
        )

    except Exception as e:
        logger.exception("skills_list error")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


def _validate_skill_view_pagination_args(
    *,
    file_path: str | None,
    offset: int | None,
    limit: int | None,
) -> dict[str, Any] | None:
    """Validate a bounded text-resource page without silently coercing it."""
    manifest_page = file_path == "__manifest__"
    max_offset = (
        MAX_SKILL_MANIFEST_OFFSET_ENTRIES
        if manifest_page else MAX_SKILL_VIEW_OFFSET_CHARS
    )
    max_limit = (
        MAX_SKILL_MANIFEST_PAGE_ENTRIES
        if manifest_page else MAX_SKILL_VIEW_PAGE_CHARS
    )
    if offset is not None and (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > max_offset
    ):
        return {
            "success": False,
            "reason": "invalid_pagination_offset",
            "error": (
                "skill_view offset must be an integer from 0 through "
                f"{max_offset}."
            ),
        }
    if limit is not None and (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > max_limit
    ):
        return {
            "success": False,
            "reason": "invalid_pagination_limit",
            "error": (
                "skill_view limit must be an integer from 1 through "
                f"{max_limit}."
            ),
        }
    if (offset is not None or limit is not None) and (
        not isinstance(file_path, str)
        or not file_path
    ):
        return {
            "success": False,
            "reason": "pagination_requires_file_resource",
            "error": (
                "skill_view offset/limit require either file_path='__manifest__' "
                "or one regular bundled Skill resource."
            ),
        }
    return None


def _load_bounded_skill_resource(
    *,
    mgr: Any,
    name: str,
    file_path: str,
    offset: int,
    limit: int,
    pagination_requested: bool,
    user_id: str,
    session_id: str,
    include_optional: bool,
    enabled_user_skills: list[str] | None,
) -> dict[str, Any]:
    """Resolve one authorized resource, then stream a bounded text page.

    Loading the Skill main document first preserves manager-owned package
    precedence, session isolation, optional-Skill visibility, and user-Skill
    enablement checks.  The linked file itself is then revalidated against the
    resolved root and streamed, so a large reference is never materialized in
    memory merely to return its first page.
    """
    loaded = mgr.load_skill(
        name=name,
        user_id=user_id,
        session_id=session_id,
        include_optional=include_optional,
        enabled_user_skills=enabled_user_skills,
    )
    if loaded.get("success") is False:
        return loaded
    skill_dir_text = loaded.get("skill_dir")
    if not isinstance(skill_dir_text, str) or not skill_dir_text:
        return {
            "success": False,
            "reason": "skill_root_unavailable",
            "error": "Resolved Skill root is unavailable.",
        }
    skill_dir = Path(skill_dir_text)
    file_check = validate_skill_resource(
        skill_dir,
        file_path,
        expected_kind="file",
        require_relative=True,
    )
    if file_check.valid and file_check.path is not None:
        return _stream_skill_text_page(
            target=file_check.path,
            skill_name=name,
            file_path=file_path,
            offset=offset,
            limit=limit,
        )

    # Preserve the existing bounded directory listing and detailed missing /
    # unsafe-path diagnostics. Character pagination is deliberately not
    # overloaded to mean directory-entry pagination.
    fallback = mgr.load_skill(
        name=name,
        file_path=file_path,
        user_id=user_id,
        session_id=session_id,
        include_optional=include_optional,
        enabled_user_skills=enabled_user_skills,
    )
    if fallback.get("success") is not False and fallback.get("is_directory"):
        if pagination_requested:
            return {
                "success": False,
                "reason": "directory_pagination_not_supported",
                "error": (
                    "skill_view offset/limit paginate text characters, not a "
                    "directory listing. Inspect one listed file_path instead."
                ),
            }
    return fallback


def _stream_skill_text_page(
    *,
    target: Path,
    skill_name: str,
    file_path: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Return one exact UTF-8 character page with whole-resource integrity.

    Decoding and hashing are incremental.  This keeps memory bounded by the
    requested page even for a large bundled reference, while still detecting
    invalid UTF-8 anywhere in the resource before claiming it is text.
    """
    try:
        before = target.stat()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        digest = hashlib.sha256()
        selected: list[str] = []
        total_chars = 0
        is_binary = False

        with target.open("rb") as stream:
            while True:
                raw = stream.read(_SKILL_RESOURCE_READ_CHUNK_BYTES)
                if not raw:
                    break
                digest.update(raw)
                if is_binary:
                    continue
                try:
                    decoded = decoder.decode(raw, final=False)
                except UnicodeDecodeError:
                    is_binary = True
                    selected.clear()
                    continue
                if decoded:
                    chunk_start = total_chars
                    chunk_end = chunk_start + len(decoded)
                    page_end = offset + limit
                    if chunk_end > offset and chunk_start < page_end:
                        left = max(0, offset - chunk_start)
                        right = min(len(decoded), page_end - chunk_start)
                        if right > left:
                            selected.append(decoded[left:right])
                    total_chars = chunk_end

        if not is_binary:
            try:
                decoded = decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                is_binary = True
                selected.clear()
            else:
                if decoded:
                    chunk_start = total_chars
                    chunk_end = chunk_start + len(decoded)
                    page_end = offset + limit
                    if chunk_end > offset and chunk_start < page_end:
                        left = max(0, offset - chunk_start)
                        right = min(len(decoded), page_end - chunk_start)
                        if right > left:
                            selected.append(decoded[left:right])
                    total_chars = chunk_end

        after = target.stat()
    except OSError as exc:
        return {
            "success": False,
            "reason": "resource_read_failed",
            "error": f"Cannot read Skill resource '{file_path}': {exc}",
        }

    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        return {
            "success": False,
            "reason": "resource_changed_during_read",
            "error": (
                f"Skill resource '{file_path}' changed while it was being read; "
                "recompile/reinstall the Skill before retrying."
            ),
        }

    size_bytes = before.st_size
    sha256 = digest.hexdigest()
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if is_binary:
        if offset != 0:
            return {
                "success": False,
                "reason": "binary_resource_not_pageable",
                "error": (
                    f"Skill resource '{file_path}' is binary and cannot be read "
                    "with a text offset. Use skill_copy_resource instead."
                ),
                "is_binary": True,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "media_type": media_type,
            }
        return {
            "success": True,
            "name": skill_name,
            "file": file_path,
            "content": f"[Binary file: {target.name}, size: {size_bytes} bytes]",
            "is_binary": True,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "media_type": media_type,
            "hint": (
                "Use skill_copy_resource with this Skill name and source path "
                "to preserve the exact bytes in the session workspace."
            ),
        }

    if offset > total_chars:
        return {
            "success": False,
            "reason": "pagination_offset_out_of_range",
            "error": (
                f"skill_view offset {offset} exceeds the resource length of "
                f"{total_chars} Unicode characters."
            ),
            "name": skill_name,
            "file": file_path,
            "total_chars": total_chars,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "media_type": media_type,
        }

    content = _bounded_serialized_content_prefix("".join(selected))
    returned_chars = len(content)
    next_offset = offset + returned_chars
    has_more = next_offset < total_chars
    pagination = {
        "unit": "unicode_codepoints",
        "offset": offset,
        "limit": limit,
        "returned_chars": returned_chars,
        "total_chars": total_chars,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    }
    result: dict[str, Any] = {
        "success": True,
        "name": skill_name,
        "file": file_path,
        "content": content,
        "file_type": target.suffix,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "media_type": media_type,
        "offset": offset,
        "limit": limit,
        "returned_chars": returned_chars,
        "total_chars": total_chars,
        "next_offset": pagination["next_offset"],
        "has_more": has_more,
        "truncated": has_more,
        "pagination": pagination,
    }
    if has_more:
        result["hint"] = (
            "This is a bounded page, not the complete resource. Continue with "
            f"skill_view(name={skill_name!r}, file_path={file_path!r}, "
            f"offset={next_offset}); do not guess or skip offsets."
        )
    return result


def _bounded_serialized_content_prefix(content: str) -> str:
    """Keep the complete JSON tool result below its history transport cap.

    A character limit alone is insufficient because quotes, backslashes, and
    control characters expand during JSON encoding.  Return the longest exact
    prefix whose encoded string representation stays bounded; ``next_offset``
    then exposes the first undisclosed Unicode character without omission.
    """
    if len(json.dumps(content, ensure_ascii=False)) <= (
        _MAX_SKILL_VIEW_SERIALIZED_CONTENT_CHARS
    ):
        return content
    low = 0
    high = len(content)
    while low < high:
        midpoint = (low + high + 1) // 2
        encoded_chars = len(json.dumps(content[:midpoint], ensure_ascii=False))
        if encoded_chars <= _MAX_SKILL_VIEW_SERIALIZED_CONTENT_CHARS:
            low = midpoint
        else:
            high = midpoint - 1
    return content[:low]


async def skill_view(
    name: str,
    file_path: str | None = None,
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """View the content of a skill or a specific file within a skill directory.

    Progressive disclosure tier 2-3: loads full SKILL.md content plus
    linked_files and resource_graph indexes. Use file_path='__manifest__'
    for a compact workflow-resource manifest, or use file_path to access
    specific linked files.

    MCP dependencies are registered by the harness control plane when a skill
    is installed. The response reports their declared configuration for
    inspection, but the model must not recreate it manually.

    Args:
        name: Name of the skill (e.g., "axolotl" or "category/axolotl").
        file_path: Optional path to a linked file within the skill
            (e.g., "references/api.md", "templates/config.yaml").
        offset: Zero-based Unicode-character offset for a text resource page.
            Valid only when ``file_path`` names one regular bundled file.
        limit: Maximum Unicode characters to return for one text resource page.
            Defaults to 40,000 and cannot exceed 100,000. The returned page
            may be smaller when JSON escaping would exceed the transport cap.
        user_id: User identifier for per-user skill isolation.
        session_id: Session identifier.

    Returns:
        JSON string with skill content or error.
    """
    try:
        pagination_error = _validate_skill_view_pagination_args(
            file_path=file_path,
            offset=offset,
            limit=limit,
        )
        if pagination_error:
            return json.dumps(pagination_error, ensure_ascii=False)

        mgr = get_manager()
        include_optional = mgr.get_session_optional(session_id)

        if file_path and file_path != "__manifest__":
            result = _load_bounded_skill_resource(
                mgr=mgr,
                name=name,
                file_path=file_path,
                offset=0 if offset is None else offset,
                limit=(
                    DEFAULT_SKILL_VIEW_PAGE_CHARS
                    if limit is None else limit
                ),
                pagination_requested=(offset is not None or limit is not None),
                user_id=user_id,
                session_id=session_id,
                include_optional=include_optional,
                enabled_user_skills=enabled_user_skills,
            )
        elif file_path == "__manifest__":
            result = mgr.load_skill(
                name=name,
                file_path=file_path,
                user_id=user_id,
                session_id=session_id,
                include_optional=include_optional,
                enabled_user_skills=enabled_user_skills,
                offset=0 if offset is None else offset,
                limit=(
                    DEFAULT_SKILL_MANIFEST_PAGE_ENTRIES
                    if limit is None else limit
                ),
            )
        else:
            result = mgr.load_skill(
                name=name,
                file_path=file_path,
                user_id=user_id,
                session_id=session_id,
                include_optional=include_optional,
                enabled_user_skills=enabled_user_skills,
            )
            # A canonical Skill body is free-form and may be much larger than
            # the generic tool-history cap.  Never let an apparently
            # successful main read disclose only an unlabeled prefix.  Keep
            # the compiler metadata from the manager, but replace a large
            # body with an integrity-addressed first page of the literal
            # SKILL.md.  Later pages use the ordinary exact file_path cursor.
            content = result.get("content") if isinstance(result, dict) else None
            if (
                result.get("success") is not False
                and isinstance(content, str)
                and len(json.dumps(content, ensure_ascii=False))
                > _MAX_SKILL_VIEW_SERIALIZED_CONTENT_CHARS
            ):
                skill_dir_text = result.get("skill_dir")
                if isinstance(skill_dir_text, str) and skill_dir_text:
                    page = _stream_skill_text_page(
                        target=Path(skill_dir_text) / "SKILL.md",
                        skill_name=name,
                        file_path="SKILL.md",
                        offset=0,
                        limit=DEFAULT_SKILL_VIEW_PAGE_CHARS,
                    )
                    if page.get("success") is False:
                        result = page
                    else:
                        result = {**result, **page}
                        result["main_document_paged"] = True
                        result["hint"] = (
                            "The canonical SKILL.md is paginated. Read every "
                            "contiguous page with file_path='SKILL.md' and the "
                            "returned next_offset before submitting a capability plan."
                        )

        if result.get("success") is False and file_path:
            candidates = _current_session_file_candidates(
                file_path,
                user_id=user_id,
                session_id=session_id,
                include_optional=include_optional,
                enabled_user_skills=enabled_user_skills,
            )
            if candidates:
                result["candidate_files"] = candidates
                result["hint"] = (
                    "The requested file was not found in that skill. Use one of candidate_files if it matches the intended current-session skill resource, "
                    "or call skill_view(name, file_path='__manifest__') to inspect the available resource graph."
                )

        # ── Auto-detect bundled MCP server scripts ───────────────────────
        # If the skill has an .mcp.json (mcp_config in linked_files) or
        # Python scripts that look like MCP servers, add a hint.
        # MCP servers are auto-registered on skill upload, so the agent
        # should try using mcp_* tools directly before manual configuration.
        if (
            result.get("success") is not False
            and not result.get("mcp_servers")
            and not file_path
        ):
            linked = result.get("linked_files") or {}

            # Check for .mcp.json (openclaw-compatible)
            has_mcp_config = "mcp_config" in linked

            all_files = []
            for category_files in linked.values():
                all_files.extend(category_files)

            mcp_scripts = []
            for f in all_files:
                if (
                    isinstance(f, str)
                    and f.endswith(".py")
                    and "mcp" in f.lower()
                ):
                    mcp_scripts.append(f)
            mcp_scripts.sort(key=lambda path: (path.casefold(), path))
            mcp_script_count = len(mcp_scripts)
            bounded_mcp_scripts = [
                path
                for path in mcp_scripts
                if _utf8_size(path) <= _MAX_ACTIVATION_RESOURCE_PATH_BYTES
            ][:_MAX_ACTIVATION_MCP_SCRIPT_HINTS]

            if has_mcp_config or mcp_scripts:
                skill_dir = result.get("skill_dir", "")

                if has_mcp_config:
                    result["mcp_config_hint"] = (
                        "This skill includes an .mcp.json file (openclaw-compatible "
                        "MCP configuration). MCP servers defined in .mcp.json are "
                        "automatically registered when the skill is uploaded. "
                        "If the mcp_* tools are not yet available, use "
                        "mcp_server_status to inspect the runtime error and "
                        "report it to the user."
                    )

                if bounded_mcp_scripts:
                    env_hints = _extract_env_hints(result.get("content", ""))
                    env_example = ""
                    if env_hints:
                        env_example = ", env=" + str(env_hints)
                    result["mcp_script_hint"] = (
                        "This skill bundles Python MCP server scripts. The runtime "
                        "registers them during skill installation. If the expected "
                        "mcp_* tools are unavailable, call mcp_server_status and "
                        "report its concrete error; do not add/remove servers from "
                        "inside the agent turn.\n\nServer scripts:\n"
                        + "\n".join(
                            f"  {skill_dir}/{script}{env_example}"
                            for script in bounded_mcp_scripts
                        )
                        + (
                            f"\n  [... {mcp_script_count - len(bounded_mcp_scripts)} "
                            "more script paths available through __manifest__]"
                            if mcp_script_count > len(bounded_mcp_scripts)
                            else ""
                        )
                        + "\n\nCRITICAL RULES:\n"
                        "- The args MUST be the full file path to the Python "
                        "script, NOT '-c', NOT '-m', NOT a module name.\n"
                        "- Do NOT invent different args. Copy the exact args "
                        "from the example above.\n"
                        + (
                            "Set the env variables shown above to the actual "
                            "service URLs before calling any MCP tools. "
                            "If you don't know the correct URL, ask the user."
                            if env_hints else
                            "Check the SKILL.md content above for required "
                            "environment variables (like API_URL). "
                            "If unsure about values, ask the user."
                        )
                    )

        if not file_path and isinstance(result, dict):
            result = _bounded_activation_resource_indexes(result)
        if (
            isinstance(result, dict)
            and (not file_path or file_path == "__manifest__")
        ):
            result = _compact_large_skill_view_envelope(
                result,
                manifest_view=(file_path == "__manifest__"),
            )

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.exception("skill_view error")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


async def skill_copy_resource(
    name: str,
    source_path: str,
    destination_path: str,
    overwrite: bool = False,
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
    context: ToolContext | None = None,
) -> str:
    """Copy one inert Skill resource into the current session workspace.

    This is the binary-safe counterpart to ``skill_view``.  It never executes
    the resource and never accepts an absolute source or destination.  The
    installed Skill's normal session/enablement rules are checked before an
    atomic copy, and the receipt includes an integrity hash for downstream
    artifact verification.
    """
    def _operation() -> str:
        workspace = validate_path(
            ".",
            user_id,
            session_id,
            sub="workspace",
        )
        with workspace_mutation_guard(workspace):
            return _skill_copy_resource_locked(
                name,
                source_path,
                destination_path,
                overwrite=overwrite,
                user_id=user_id,
                session_id=session_id,
                enabled_user_skills=enabled_user_skills,
                context=context,
            )

    return await run_sync_cancellation_safe(_operation)


def _skill_copy_resource_locked(
    name: str,
    source_path: str,
    destination_path: str,
    *,
    overwrite: bool,
    user_id: str,
    session_id: str,
    enabled_user_skills: list[str] | None,
    context: ToolContext | None = None,
) -> str:
    """Perform the final no-clobber copy under the workspace mutation lock."""

    try:
        loaded = get_manager().load_skill(
            name=name,
            user_id=user_id,
            session_id=session_id,
            include_optional=get_manager().get_session_optional(session_id),
            enabled_user_skills=enabled_user_skills,
        )
        if loaded.get("success") is False:
            return json.dumps(loaded, ensure_ascii=False)
        skill_dir_text = loaded.get("skill_dir")
        if not isinstance(skill_dir_text, str) or not skill_dir_text:
            return json.dumps(
                {"success": False, "error": "Resolved Skill root is unavailable."},
                ensure_ascii=False,
            )
        source_check = validate_skill_resource(
            Path(skill_dir_text),
            source_path,
            expected_kind="file",
            require_relative=True,
        )
        if not source_check.valid or source_check.path is None:
            return json.dumps(
                {
                    "success": False,
                    "error": source_check.message or "Unsafe or missing Skill resource.",
                    "reason": source_check.code,
                },
                ensure_ascii=False,
            )
        source = source_check.path
        clean_destination = str(destination_path or "")
        if clean_destination.startswith("workspace/"):
            clean_destination = clean_destination[len("workspace/"):]
        destination = validate_path(
            clean_destination,
            user_id,
            session_id,
            sub="workspace",
        )
        if destination.exists() and not overwrite:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Destination already exists: {destination_path}",
                    "hint": "Set overwrite=true only when replacing that exact workspace artifact is intended.",
                },
                ensure_ascii=False,
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        # Revalidate after parent creation so a linked component introduced in
        # between the two operations cannot redirect the final rename.
        destination = validate_path(
            clean_destination,
            user_id,
            session_id,
            sub="workspace",
        )
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".skill-copy.tmp",
            dir=str(destination.parent),
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as input_stream, os.fdopen(fd, "wb") as output_stream:
                while True:
                    chunk = input_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    output_stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            temp_path = Path(temp_name)
            require_execution_authority(
                context,
                boundary="workspace.skill_copy.commit",
            )
            if overwrite:
                os.replace(temp_path, destination)
            else:
                # A hard link provides atomic no-clobber semantics within the
                # destination directory; the temporary inode is then removed.
                os.link(temp_path, destination)
                temp_path.unlink()
            try:
                destination.chmod(0o644)
            except OSError:
                pass
        except Exception:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
            raise

        return json.dumps(
            {
                "success": True,
                "source_tool": "skill_copy_resource",
                "skill": name,
                "source_path": source_path,
                "filepath": clean_destination,
                "size_bytes": size,
                "sha256": digest.hexdigest(),
                "is_binary_safe_copy": True,
            },
            ensure_ascii=False,
        )
    except FileExistsError:
        return json.dumps(
            {
                "success": False,
                "error": f"Destination already exists: {destination_path}",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.exception("skill_copy_resource error")
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)




def _current_session_file_candidates(
    requested_path: str,
    *,
    user_id: str,
    session_id: str,
    include_optional: bool,
    enabled_user_skills: list[str] | None,
) -> list[dict[str, str]]:
    requested = PurePosixPath(requested_path)
    if requested.is_absolute() or ".." in requested.parts:
        return []
    basename = requested.name.lower()
    requested_text = str(requested).lower()
    skills = find_all_skills(
        user_id,
        session_id,
        include_optional=include_optional,
        enabled_user_skills=enabled_user_skills,
    )
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for skill in skills:
        skill_name = str(skill.get("name") or "")
        skill_path = skill.get("path")
        if not skill_name or not isinstance(skill_path, str):
            continue
        root = Path(skill_path).parent.resolve()
        for path in sorted(root.rglob("*")):
            if len(candidates) >= MAX_CANDIDATE_FILES:
                return candidates
            if path.is_symlink() or not path.is_file() or path.name == "SKILL.md":
                continue
            try:
                resolved = path.resolve()
                rel = str(PurePosixPath(resolved.relative_to(root)))
            except (OSError, ValueError):
                continue
            rel_lower = rel.lower()
            if not _candidate_file_matches(requested_text, basename, rel_lower):
                continue
            key = (skill_name, rel)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"skill": skill_name, "file_path": rel})
    return candidates


def _candidate_file_matches(requested_text: str, basename: str, rel_lower: str) -> bool:
    rel_name = PurePosixPath(rel_lower).name
    if basename and rel_name == basename:
        return True
    if requested_text and requested_text in rel_lower:
        return True
    return any(keyword in rel_lower for keyword in CANDIDATE_FILE_KEYWORDS) and any(part in rel_lower for part in ("report", "md", "py", "yaml", "json", "csv"))


# ── JSON Schemas for registry ──────────────────────────────────────────────

SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": (
        "List all available skills with name and description. "
        "Use skill_view(name) to load a skill's full content before using it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results.",
            },
        },
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": (
        "Load a skill's full content including instructions, tags, linked files, and a "
        "generic resource graph. First call returns SKILL.md content plus indexes. "
        "For complex tasks, call again with file_path='__manifest__' to inspect "
        "workflow resources, then call with specific file paths to load orchestrators, "
        "workers, references, scripts, templates, or other linked resources. Text "
        "resources are returned in bounded Unicode-character pages. Resource manifests "
        "are returned in bounded entry pages with a stable manifest_sha256. In either "
        "case, continue only from the exact next_offset returned by the prior page."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (use skills_list to see available skills).",
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Optional path to a linked file within the skill. Use '__manifest__' "
                    "to inspect the skill's compact resource graph, then request files such as "
                    "'orchestration/orchestrator.yaml', 'references/api.md', or "
                    "'templates/config.yaml'. Omit to get the main SKILL.md content."
                ),
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_SKILL_VIEW_OFFSET_CHARS,
                "description": (
                    "Zero-based Unicode-character offset for one text resource, or "
                    "zero-based resource-entry offset with file_path='__manifest__'. "
                    "Use only the exact next_offset returned by the preceding page. "
                    "Omit for the first page."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SKILL_VIEW_PAGE_CHARS,
                "default": DEFAULT_SKILL_VIEW_PAGE_CHARS,
                "description": (
                    "Maximum Unicode characters in one linked text-resource page, or "
                    f"manifest entries (maximum {MAX_SKILL_MANIFEST_PAGE_ENTRIES}) when "
                    "file_path='__manifest__'. Text-resource limit "
                    f"(default {DEFAULT_SKILL_VIEW_PAGE_CHARS}, maximum "
                    f"{MAX_SKILL_VIEW_PAGE_CHARS}). The actual page can be "
                    "smaller when JSON escaping requires it."
                ),
            },
        },
        "required": ["name"],
    },
}


SKILL_COPY_RESOURCE_SCHEMA = {
    "name": "skill_copy_resource",
    "description": (
        "Copy one installed Skill resource byte-for-byte into the current session workspace. "
        "Use this for binary assets or exact templates (PDF, XLSX, DOCX, images, archives, or "
        "other files that skill_view cannot render as text). This tool copies only; it never "
        "executes the resource."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Installed Skill name from skills_list.",
            },
            "source_path": {
                "type": "string",
                "description": "Relative path inside the Skill package.",
            },
            "destination_path": {
                "type": "string",
                "description": "Relative destination path inside the current workspace.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Replace the exact destination when it already exists. Defaults to false.",
                "default": False,
            },
        },
        "required": ["name", "source_path", "destination_path"],
    },
}
