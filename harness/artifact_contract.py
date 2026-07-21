"""Deterministic verification for compiled artifact output contracts.

This module deliberately has no dependency on the agent loop or run state.  It
checks the current workspace snapshot against a generic compiled
``output_contract`` dictionary and returns stable, machine-readable findings.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import stat
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import unquote
from xml.etree import ElementTree

import yaml

from workspace_patterns import (
    WorkspacePatternError,
    normalize_workspace_pattern,
    workspace_pattern_matches,
)


_REMOTE_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_MIME_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_PENDING_RE = re.compile(
    r"(?<![\w-])(?:TODO|TBD|FIXME)(?![\w-])|"
    r"(?:^|\s)-\s*\[\s*\]|"
    r"待执行|待填写|⏳",
    re.IGNORECASE | re.MULTILINE,
)
_STATUS_FAILURE_RE = re.compile(
    r"✗|❌|⏳|\b(?:fail(?:ed|ure)?|incomplete|pending|blocked|missing|"
    r"todo|tbd|fixme)\b|未完成|失败|阻塞|缺失|待执行|待填写",
    re.IGNORECASE,
)
_STATUS_DEGRADED_RE = re.compile(r"\b(?:warn(?:ing)?|degraded)\b|降级|警告", re.IGNORECASE)
_STATUS_PASS_RE = re.compile(
    r"✓|✅|\[x\]|\b(?:pass(?:ed)?|complete(?:d)?|done|ok|success)\b|通过|完成",
    re.IGNORECASE,
)
_STATUS_HEADERS = ("status", "state", "result", "状态", "结果")
_MERGE_ROW_RE = re.compile(
    r"auto[- ]?merge|\bmerge\b|concatenat|full\s+report.{0,32}generat|自动合并|完整报告.{0,20}生成",
    re.IGNORECASE,
)
_INTERNAL_WORKSPACE_COMPONENTS = {
    ".chatds",
    ".pytest_cache",
    "__pycache__",
    "debug",
}

# Workspace enumeration is an adversarial boundary: a session can contain an
# unexpectedly deep tree or millions of directory entries.  Keep all budgets
# independent of any particular artifact format.  A budget violation is a
# verifier failure (never a silently truncated snapshot).
_MAX_WORKSPACE_SCAN_ENTRIES = 10_000
_MAX_WORKSPACE_SCAN_DEPTH = 64
_MAX_WORKSPACE_RELATIVE_PATH_BYTES = 4_096
_MAX_WORKSPACE_SCAN_PATH_BYTES = 4_000_000


def _merge_is_required(contract: Mapping[str, Any]) -> bool:
    """Resolve explicit merge policy with the bounded legacy command fallback."""

    mandatory = contract.get("merge_mandatory")
    if isinstance(mandatory, bool):
        return mandatory
    command = contract.get("merge_command")
    return isinstance(command, str) and bool(command.strip())


def verify_artifact_contract(
    workspace: Path,
    output_contract: Mapping[str, Any],
    *,
    baseline_paths: Iterable[str] | None = None,
    receipt_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Verify a workspace against a compiled output contract.

    The function is read-only and deterministic.  Normal contract violations
    are returned as findings rather than raised.  ``baseline_paths`` and
    ``receipt_paths`` are optional run-state facts used to distinguish prior
    workspace inputs from outputs created or modified by the current run; the
    legacy two-argument snapshot behavior remains available when neither is
    supplied.  Callers integrate it by checking ``result["valid"]``.
    """

    root = Path(workspace).resolve()
    contract = _normalized_contract(output_contract)
    findings: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}

    if not root.is_dir():
        _add_finding(
            findings,
            "workspace_missing",
            "Workspace does not exist or is not a directory.",
            actual=str(root),
        )
        return {
            "valid": False,
            "findings": findings,
            "metrics": metrics,
            "manifest": [],
        }

    files, symlinks, scan_findings, scan_metrics = _workspace_files(root)
    metrics["workspace_scan"] = scan_metrics
    if scan_findings:
        # A partial traversal is not an authoritative workspace snapshot.  Do
        # not continue contract matching or expose a partial manifest because
        # either could incorrectly certify an output that happened to occur in
        # the scanned prefix.
        findings.extend(scan_findings)
        return {
            "valid": False,
            "findings": findings,
            "metrics": metrics,
            "manifest": [],
        }
    baseline = _normalize_snapshot_paths(baseline_paths)
    receipts = _normalize_snapshot_paths(receipt_paths)
    produced_paths = _produced_paths(
        files,
        baseline_paths=baseline,
        receipt_paths=receipts,
        baseline_supplied=baseline_paths is not None,
        receipts_supplied=receipt_paths is not None,
    )
    resolution_files = (
        files
        if produced_paths is None
        else [
            (relative, path)
            for relative, path in files
            if relative.casefold() in produced_paths
        ]
    )
    manifest = [
        {
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for relative, path in files
    ]
    for relative in symlinks:
        if baseline_paths is not None and relative.casefold() in baseline:
            continue
        _add_finding(
            findings,
            "artifact_symlink_rejected",
            "Workspace artifact verification does not follow symlinks.",
            artifact=relative,
        )

    route_deliverables = _string_list(contract.get("declared_artifacts"))
    modular = _string_list(contract.get("declared_modular_files"))
    ancillary = _string_list(contract.get("declared_ancillary_files"))
    final_declaration = _string_value(contract.get("declared_final_artifact"))
    policy = contract.get("artifact_set_policy")
    if not isinstance(policy, Mapping):
        policy = {}

    declared = route_deliverables + modular + ancillary
    if final_declaration:
        declared.append(final_declaration)
    # An exact artifact policy is authoritative too: an artifact present only
    # in that list is still required, while semantic declarations retain their
    # order for merge verification.
    declared = _dedupe_strings(declared + _string_list(policy.get("artifacts")))

    resolved: dict[str, Path] = {}
    resolved_relative: dict[str, str] = {}
    declaration_by_relative: dict[str, str] = {}
    for declaration in declared:
        try:
            normalize_workspace_pattern(declaration)
        except WorkspacePatternError as exc:
            _add_finding(
                findings,
                "declared_artifact_pattern_invalid",
                "A Skill-declared artifact pattern is not a safe workspace-relative pattern.",
                artifact=declaration,
                actual=str(exc),
            )
            continue
        matches = _matching_files(resolution_files, declaration)
        if not matches:
            _add_finding(
                findings,
                "declared_artifact_missing",
                "A Skill-declared artifact is missing from the workspace.",
                artifact=declaration,
            )
            continue
        if len(matches) > 1:
            _add_finding(
                findings,
                "declared_artifact_ambiguous",
                "A Skill-declared artifact pattern resolves to more than one file.",
                artifact=declaration,
                actual=[relative for relative, _ in matches],
            )
            continue
        relative, path = matches[0]
        previous_declaration = declaration_by_relative.get(relative.casefold())
        if previous_declaration is not None and previous_declaration != declaration:
            _add_finding(
                findings,
                "declared_artifact_ambiguous",
                "Two Skill declarations resolve to the same workspace file.",
                artifact=declaration,
                actual={
                    "path": relative,
                    "other_declaration": previous_declaration,
                },
            )
            continue
        declaration_by_relative[relative.casefold()] = declaration
        resolved[declaration] = path
        resolved_relative[declaration] = relative

    _verify_artifact_set(
        files,
        declared,
        contract,
        policy,
        findings,
        metrics,
        produced_paths=produced_paths,
    )

    completion_metrics = _verify_completion_bounds(
        final_declaration,
        declared,
        resolved,
        resolved_relative,
        contract,
        findings,
    )
    if completion_metrics:
        metrics["completion"] = completion_metrics

    merge_metrics = _verify_merge(
        modular,
        final_declaration,
        resolved,
        resolved_relative,
        contract,
        findings,
    )
    if merge_metrics:
        metrics["merge"] = merge_metrics

    index_declaration = _artifact_index_declaration(contract, ancillary)
    if index_declaration:
        index_path = resolved.get(index_declaration)
        if index_path is None:
            matches = _matching_files(resolution_files, index_declaration)
            if len(matches) == 1:
                index_path = matches[0][1]
        if index_path is not None:
            _verify_readme_links(
                root,
                index_path,
                declared,
                resolved_relative,
                contract,
                findings,
            )

    quality_contract = _quality_contract(contract)
    checklist_policy = _checklist_policy(quality_contract)
    checklist_declaration = _checklist_declaration(
        contract,
        ancillary,
        checklist_policy,
    )
    if checklist_declaration:
        checklist_path = resolved.get(checklist_declaration)
        if checklist_path is None:
            matches = _matching_files(resolution_files, checklist_declaration)
            if len(matches) == 1:
                checklist_path = matches[0][1]
        if checklist_path is not None:
            checklist_metrics = _verify_checklist(
                checklist_path,
                contract,
                checklist_policy,
                merge_metrics,
                findings,
            )
            metrics["checklist"] = checklist_metrics

    content_paths = _unique_paths(
        [resolved[item] for item in modular if item in resolved]
        + ([resolved[final_declaration]] if final_declaration in resolved else [])
    )
    pending_policy = _pending_marker_policy(quality_contract)
    if pending_policy is not None:
        for declaration in declared:
            path = resolved.get(declaration)
            if path is None or not _is_plain_text_artifact(path, contract):
                continue
            _verify_pending_markers(
                path,
                root,
                findings,
                markers=pending_policy,
            )
    for path in _unique_paths(list(resolved.values())):
        _verify_nonempty_artifact(path, root, findings)
    raw_formats = contract.get("artifact_formats")
    if "artifact_formats" in contract and not isinstance(raw_formats, Mapping):
        _add_finding(
            findings,
            "artifact_formats_invalid",
            "artifact_formats must be a mapping of artifact paths to format names.",
            actual=raw_formats,
        )
    else:
        format_metrics = _verify_declared_formats(
            root,
            declared,
            resolved,
            raw_formats,
            findings,
        )
        if format_metrics:
            metrics["formats"] = format_metrics
    validator_metrics = _verify_declared_validators(
        root,
        declared,
        resolved,
        contract.get("artifact_validators"),
        findings,
    )
    if validator_metrics:
        metrics["validators"] = validator_metrics
    if _padding_checks_enabled(quality_contract):
        for path in content_paths:
            if _is_plain_text_artifact(path, contract):
                _verify_minimum_padding(path, root, findings)

    findings.sort(
        key=lambda item: (
            str(item.get("code") or ""),
            str(item.get("artifact") or ""),
            str(item.get("message") or ""),
        )
    )
    return {
        "valid": not findings,
        "findings": findings,
        "metrics": metrics,
        "manifest": manifest,
    }


def _normalized_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    contract = dict(value) if isinstance(value, Mapping) else {}
    nested = contract.get("output_contract")
    if isinstance(nested, Mapping) and not any(
        key in contract
        for key in (
            "declared_modular_files",
            "declared_ancillary_files",
            "declared_final_artifact",
            "declared_artifacts",
        )
    ):
        normalized = dict(nested)
        quality = contract.get("quality_contract")
        if isinstance(quality, Mapping):
            normalized["quality_contract"] = dict(quality)
            constraints = quality.get("constraints")
            if isinstance(constraints, Mapping):
                for key in ("declared_checklist_rows",):
                    if key in constraints and key not in normalized:
                        normalized[key] = constraints[key]
        return normalized
    return contract


def _quality_contract(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    value = contract.get("quality_contract")
    return value if isinstance(value, Mapping) else {}


def _normalize_snapshot_paths(values: Iterable[str] | None) -> set[str]:
    """Normalize an injected run snapshot without interpreting path patterns."""

    normalized: set[str] = set()
    if values is None or isinstance(values, (str, bytes)):
        return normalized
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        try:
            pattern = normalize_workspace_pattern(candidate)
        except WorkspacePatternError:
            continue
        # Baselines and receipts identify concrete files, never globs or
        # unresolved output templates.
        if any(token in pattern for token in ("*", "?", "[", "]", "{", "}")):
            continue
        normalized.add(pattern.casefold())
    return normalized


def _produced_paths(
    files: list[tuple[str, Path]],
    *,
    baseline_paths: set[str],
    receipt_paths: set[str],
    baseline_supplied: bool,
    receipts_supplied: bool,
) -> set[str] | None:
    """Return the known run-output set, or ``None`` for legacy snapshots.

    A baseline distinguishes pre-existing inputs from files created this run.
    Receipts additionally identify files modified in place after the baseline.
    If only receipts are available they are authoritative; the verifier does
    not guess which unrelated workspace files are outputs.
    """

    current = {relative.casefold() for relative, _ in files}
    if baseline_supplied:
        return ((current - baseline_paths) | receipt_paths) & current
    if receipts_supplied:
        return receipt_paths & current
    return None


def _normalize_artifact_format(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    aliases = {
        "application/json": "json",
        "text/json": "json",
        "text/csv": "csv",
        "application/csv": "csv",
        "application/yaml": "yaml",
        "text/yaml": "yaml",
        "application/x-yaml": "yaml",
        "yml": "yaml",
        "text/markdown": "markdown",
        "md": "markdown",
        "text/plain": "text",
        "txt": "text",
        "text/html": "html",
        "application/xhtml+xml": "html",
        "image/svg+xml": "svg",
        "application/xml": "xml",
        "text/xml": "xml",
        "application/pdf": "pdf",
        "application/zip": "zip",
        "application/octet-stream": "binary",
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/gif": "gif",
        "image/webp": "webp",
        "jpg": "jpeg",
        "xlsm": "xlsx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    }
    return aliases.get(normalized, normalized)


def _verify_completion_bounds(
    final_declaration: str,
    declared: list[str],
    resolved: Mapping[str, Path],
    resolved_relative: Mapping[str, str],
    contract: Mapping[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply Skill-declared size/line bounds independent of file format.

    Completion bounds describe the declared final artifact.  For a package
    with no explicit final, they also have an unambiguous meaning when exactly
    one artifact is declared.  Missing or ambiguous declarations are reported
    by the ordinary artifact-resolution checks, so this helper simply skips
    them rather than inventing a target.
    """

    declaration = final_declaration
    if not declaration and len(declared) == 1:
        declaration = declared[0]
    path = resolved.get(declaration) if declaration else None
    if path is None:
        return {}

    relative = resolved_relative.get(declaration, path.name)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return {}
    metrics: dict[str, Any] = {
        "artifact": relative,
        "bytes": size_bytes,
    }

    min_bytes = contract.get("expected_min_bytes")
    if isinstance(min_bytes, int) and min_bytes > 0 and size_bytes < min_bytes:
        _add_finding(
            findings,
            "artifact_min_bytes_not_met",
            "The declared completion artifact is smaller than the Skill-declared minimum.",
            artifact=relative,
            expected=min_bytes,
            actual=size_bytes,
        )
    max_bytes = contract.get("expected_max_bytes")
    if isinstance(max_bytes, int) and max_bytes > 0 and size_bytes > max_bytes:
        _add_finding(
            findings,
            "artifact_max_bytes_exceeded",
            "The declared completion artifact is larger than the Skill-declared maximum.",
            artifact=relative,
            expected=max_bytes,
            actual=size_bytes,
        )

    min_lines = contract.get("expected_min_lines")
    max_lines = contract.get("expected_max_lines")
    if (
        isinstance(min_lines, int) and min_lines > 0
    ) or (
        isinstance(max_lines, int) and max_lines > 0
    ):
        line_count = _binary_line_count(path)
        if line_count is not None:
            metrics["lines"] = line_count
            if (
                isinstance(min_lines, int)
                and min_lines > 0
                and line_count < min_lines
            ):
                _add_finding(
                    findings,
                    "artifact_min_lines_not_met",
                    "The declared completion artifact has fewer lines than the Skill-declared minimum.",
                    artifact=relative,
                    expected=min_lines,
                    actual=line_count,
                )
            if (
                isinstance(max_lines, int)
                and max_lines > 0
                and line_count > max_lines
            ):
                _add_finding(
                    findings,
                    "artifact_max_lines_exceeded",
                    "The declared completion artifact has more lines than the Skill-declared maximum.",
                    artifact=relative,
                    expected=max_lines,
                    actual=line_count,
                )
    return metrics


def _binary_line_count(path: Path) -> int | None:
    """Count logical lines without assuming a text encoding or loading a file."""

    count = 0
    saw_data = False
    last_byte = b""
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                saw_data = True
                count += chunk.count(b"\n")
                last_byte = chunk[-1:]
    except OSError:
        return None
    if saw_data and last_byte != b"\n":
        count += 1
    return count


def _verify_declared_formats(
    root: Path,
    declared: list[str],
    resolved: Mapping[str, Path],
    raw_formats: Any,
    findings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate content only for formats explicitly declared by the Skill."""
    if not isinstance(raw_formats, Mapping):
        return {}
    text_formats = {
        "json", "csv", "yaml", "markdown", "text", "html", "svg", "xml",
    }
    binary_formats = {
        "binary", "pdf", "zip", "docx", "xlsx", "pptx",
        "png", "jpeg", "gif", "webp", "parquet",
    }
    supported_formats = text_formats | binary_formats
    metrics: dict[str, dict[str, Any]] = {}
    declared_by_folded = {
        item.casefold(): item for item in declared
    }
    for raw_declaration, raw_format in raw_formats.items():
        if not isinstance(raw_declaration, str) or not isinstance(raw_format, str):
            _add_finding(
                findings,
                "artifact_format_declaration_invalid",
                "Artifact format declarations require string paths and format names.",
                actual={"artifact": raw_declaration, "format": raw_format},
            )
            continue
        declaration = declared_by_folded.get(raw_declaration.casefold())
        if declaration is None:
            _add_finding(
                findings,
                "artifact_format_declaration_unbound",
                "An artifact format declaration does not identify a declared artifact.",
                artifact=raw_declaration,
            )
            continue
        artifact_format = _normalize_artifact_format(raw_format)
        opaque_mime = (
            artifact_format not in supported_formats
            and bool(_MIME_TYPE_RE.fullmatch(raw_format.strip()))
        )
        if artifact_format not in supported_formats and not opaque_mime:
            _add_finding(
                findings,
                "artifact_format_unsupported",
                "The declared artifact format has no deterministic verifier.",
                artifact=raw_declaration,
                actual=raw_format,
            )
            continue
        path = resolved.get(declaration)
        if path is None:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        metrics[relative] = {
            "declared_format": raw_format,
            "format": "opaque" if opaque_mime else artifact_format,
            "mime_type": _declared_mime_type(raw_format, artifact_format, path),
            "bytes": size,
            "sha256": _sha256_file(path),
        }
        if opaque_mime:
            # Opaque application formats have no safe built-in parser.  Their
            # deterministic minimum contract is MIME identity, non-empty
            # bytes, and the content hash reported above.  Skills can add a
            # stronger declarative ``artifact_validators`` entry.
            continue
        if artifact_format in binary_formats:
            error = _binary_format_error(path, artifact_format)
            if error:
                _add_finding(
                    findings,
                    f"invalid_{artifact_format}_artifact",
                    "A Skill-declared binary artifact failed deterministic format validation.",
                    artifact=path.relative_to(root).as_posix(),
                    actual=error,
                )
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            _add_finding(
                findings,
                "artifact_format_unreadable",
                "A format-declared artifact is not readable as UTF-8 text.",
                artifact=path.relative_to(root).as_posix(),
                actual=str(exc),
            )
            continue
        if artifact_format == "json":
            try:
                json.loads(content)
            except (json.JSONDecodeError, TypeError) as exc:
                _add_finding(
                    findings,
                    "invalid_json_artifact",
                    "A Skill-declared JSON artifact is not valid JSON.",
                    artifact=path.relative_to(root).as_posix(),
                    actual=str(exc),
                )
        elif artifact_format == "csv":
            try:
                rows = list(csv.reader(io.StringIO(content), strict=True))
            except (csv.Error, UnicodeError) as exc:
                _add_finding(
                    findings,
                    "invalid_csv_artifact",
                    "A Skill-declared CSV artifact could not be parsed.",
                    artifact=path.relative_to(root).as_posix(),
                    actual=str(exc),
                )
                continue
            if not rows or not any(any(cell.strip() for cell in row) for row in rows):
                _add_finding(
                    findings,
                    "empty_csv_artifact",
                    "A Skill-declared CSV artifact contains no data cells.",
                    artifact=path.relative_to(root).as_posix(),
                )
        elif artifact_format == "yaml":
            try:
                parsed_yaml = yaml.safe_load(content)
            except yaml.YAMLError as exc:
                _add_finding(
                    findings,
                    "invalid_yaml_artifact",
                    "A Skill-declared YAML artifact is not valid YAML.",
                    artifact=path.relative_to(root).as_posix(),
                    actual=str(exc),
                )
            else:
                if parsed_yaml is None:
                    _add_finding(
                        findings,
                        "empty_yaml_artifact",
                        "A Skill-declared YAML artifact contains no document value.",
                        artifact=relative,
                    )
        elif artifact_format == "html":
            error = _html_format_error(content)
            if error:
                _add_finding(
                    findings,
                    (
                        "empty_html_artifact"
                        if not content.strip()
                        else "invalid_html_artifact"
                    ),
                    "A Skill-declared HTML artifact failed deterministic parsing.",
                    artifact=relative,
                    actual=error,
                )
        elif artifact_format in {"svg", "xml"}:
            error = _xml_format_error(content, require_svg=artifact_format == "svg")
            if error:
                _add_finding(
                    findings,
                    f"invalid_{artifact_format}_artifact",
                    f"A Skill-declared {artifact_format.upper()} artifact failed deterministic parsing.",
                    artifact=relative,
                    actual=error,
                )
        elif artifact_format in {"markdown", "text"} and not content.strip():
            _add_finding(
                findings,
                f"empty_{artifact_format}_artifact",
                f"A Skill-declared {artifact_format} artifact is empty.",
                artifact=relative,
            )
    return metrics


_FORMAT_MIME_TYPES = {
    "json": "application/json",
    "csv": "text/csv",
    "yaml": "application/yaml",
    "markdown": "text/markdown",
    "text": "text/plain",
    "html": "text/html",
    "svg": "image/svg+xml",
    "xml": "application/xml",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "parquet": "application/vnd.apache.parquet",
    "binary": "application/octet-stream",
}


def _declared_mime_type(raw_format: str, artifact_format: str, path: Path) -> str:
    raw = str(raw_format or "").strip().casefold()
    if _MIME_TYPE_RE.fullmatch(raw):
        return raw
    if artifact_format in _FORMAT_MIME_TYPES:
        return _FORMAT_MIME_TYPES[artifact_format]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


class _BoundedHTMLParser(HTMLParser):
    """A non-rendering HTML parser that records whether markup was present."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tag_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag_count += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag_count += 1


def _html_format_error(content: str) -> str | None:
    if not content.strip():
        return "artifact is empty"
    if "\x00" in content:
        return "HTML contains a NUL byte"
    parser = _BoundedHTMLParser()
    try:
        parser.feed(content)
        parser.close()
    except (ValueError, AssertionError) as exc:
        return str(exc)
    if parser.tag_count <= 0:
        return "HTML contains no element markup"
    return None


def _xml_format_error(content: str, *, require_svg: bool) -> str | None:
    if not content.strip():
        return "artifact is empty"
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        return str(exc)
    if require_svg and str(root.tag).rsplit("}", 1)[-1].casefold() != "svg":
        return "root element is not <svg>"
    return None


def _verify_declared_validators(
    root: Path,
    declared: list[str],
    resolved: Mapping[str, Path],
    raw_validators: Any,
    findings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Apply bounded, data-only validators declared by a Skill.

    Validator declarations never execute code.  They provide a portable
    strengthening layer for opaque or domain-specific artifacts while keeping
    the verifier deterministic and side-effect free.
    """

    if raw_validators is None:
        return {}
    if not isinstance(raw_validators, Mapping):
        _add_finding(
            findings,
            "artifact_validators_invalid",
            "artifact_validators must map declared artifact paths to validator specifications.",
            actual=raw_validators,
        )
        return {}
    declared_by_folded = {item.casefold(): item for item in declared}
    metrics: dict[str, dict[str, Any]] = {}
    for raw_declaration, raw_specification in list(raw_validators.items())[:256]:
        if not isinstance(raw_declaration, str):
            _add_finding(
                findings,
                "artifact_validator_declaration_invalid",
                "Artifact validator paths must be strings.",
                actual=raw_declaration,
            )
            continue
        declaration = declared_by_folded.get(raw_declaration.casefold())
        if declaration is None:
            _add_finding(
                findings,
                "artifact_validator_declaration_unbound",
                "An artifact validator does not identify a declared artifact.",
                artifact=raw_declaration,
            )
            continue
        if isinstance(raw_specification, str):
            specification: Mapping[str, Any] = {"format": raw_specification}
        elif isinstance(raw_specification, Mapping):
            specification = raw_specification
        else:
            _add_finding(
                findings,
                "artifact_validator_specification_invalid",
                "An artifact validator must be a format string or a mapping.",
                artifact=raw_declaration,
                actual=raw_specification,
            )
            continue
        supported_fields = {
            "format",
            "mime_type",
            "non_empty",
            "min_bytes",
            "max_bytes",
            "sha256",
            "json_root_type",
            "required_json_keys",
        }
        unsupported_fields = sorted(
            str(field)
            for field in specification
            if not isinstance(field, str) or field not in supported_fields
        )
        if unsupported_fields:
            _add_finding(
                findings,
                "artifact_validator_field_unsupported",
                "Artifact validators are deterministic data checks; unsupported fields cannot be ignored.",
                artifact=raw_declaration,
                actual=unsupported_fields,
            )
        path = resolved.get(declaration)
        if path is None:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        digest = _sha256_file(path)
        metric: dict[str, Any] = {"bytes": size, "sha256": digest}
        metrics[relative] = metric

        non_empty = specification.get("non_empty", True)
        if not isinstance(non_empty, bool):
            _add_finding(
                findings,
                "artifact_validator_specification_invalid",
                "validator.non_empty must be a boolean.",
                artifact=relative,
                actual=non_empty,
            )
        elif non_empty and size <= 0:
            _add_finding(
                findings,
                "artifact_validator_non_empty_failed",
                "A declared artifact validator requires non-empty content.",
                artifact=relative,
            )

        min_bytes = specification.get("min_bytes")
        if min_bytes is not None:
            if not isinstance(min_bytes, int) or isinstance(min_bytes, bool) or min_bytes < 0:
                _add_finding(
                    findings,
                    "artifact_validator_specification_invalid",
                    "validator.min_bytes must be a non-negative integer.",
                    artifact=relative,
                    actual=min_bytes,
                )
            elif size < min_bytes:
                _add_finding(
                    findings,
                    "artifact_validator_min_bytes_failed",
                    "Artifact is smaller than its declared validator minimum.",
                    artifact=relative,
                    expected=min_bytes,
                    actual=size,
                )
        max_bytes = specification.get("max_bytes")
        if max_bytes is not None:
            if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
                _add_finding(
                    findings,
                    "artifact_validator_specification_invalid",
                    "validator.max_bytes must be a non-negative integer.",
                    artifact=relative,
                    actual=max_bytes,
                )
            elif size > max_bytes:
                _add_finding(
                    findings,
                    "artifact_validator_max_bytes_failed",
                    "Artifact is larger than its declared validator maximum.",
                    artifact=relative,
                    expected=max_bytes,
                    actual=size,
                )

        expected_hash = specification.get("sha256")
        if expected_hash is not None:
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-fA-F]{64}", expected_hash.strip()
            ):
                _add_finding(
                    findings,
                    "artifact_validator_specification_invalid",
                    "validator.sha256 must be one 64-character hexadecimal digest.",
                    artifact=relative,
                    actual=expected_hash,
                )
            elif digest.casefold() != expected_hash.strip().casefold():
                _add_finding(
                    findings,
                    "artifact_validator_sha256_failed",
                    "Artifact content does not match its declared SHA-256 digest.",
                    artifact=relative,
                    expected=expected_hash.strip().casefold(),
                    actual=digest,
                )

        raw_mime = specification.get("mime_type")
        if raw_mime is not None:
            if not isinstance(raw_mime, str) or not _MIME_TYPE_RE.fullmatch(raw_mime.strip()):
                _add_finding(
                    findings,
                    "artifact_validator_specification_invalid",
                    "validator.mime_type must be a syntactically valid MIME type.",
                    artifact=relative,
                    actual=raw_mime,
                )
            else:
                metric["mime_type"] = raw_mime.strip().casefold()

        raw_format = specification.get("format")
        if raw_format is not None:
            if not isinstance(raw_format, str) or not raw_format.strip():
                _add_finding(
                    findings,
                    "artifact_validator_specification_invalid",
                    "validator.format must be a non-empty format string.",
                    artifact=relative,
                    actual=raw_format,
                )
            else:
                nested_metrics = _verify_declared_formats(
                    root,
                    [declaration],
                    {declaration: path},
                    {declaration: raw_format},
                    findings,
                )
                if relative in nested_metrics:
                    metric.update(nested_metrics[relative])

        required_keys = specification.get("required_json_keys")
        root_type = specification.get("json_root_type")
        if required_keys is not None or root_type is not None:
            _verify_json_validator(
                path,
                relative,
                required_keys=required_keys,
                root_type=root_type,
                findings=findings,
            )
    if len(raw_validators) > 256:
        _add_finding(
            findings,
            "artifact_validators_limit_exceeded",
            "artifact_validators exceeds the deterministic item limit.",
            expected=256,
            actual=len(raw_validators),
        )
    return metrics


def _verify_json_validator(
    path: Path,
    artifact: str,
    *,
    required_keys: Any,
    root_type: Any,
    findings: list[dict[str, Any]],
) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _add_finding(
            findings,
            "artifact_validator_json_failed",
            "Artifact could not be parsed for its declared JSON validator.",
            artifact=artifact,
            actual=str(exc),
        )
        return
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if root_type is not None:
        expected_type = type_map.get(str(root_type).strip().casefold())
        if expected_type is None:
            _add_finding(
                findings,
                "artifact_validator_specification_invalid",
                "validator.json_root_type must name a JSON root type.",
                artifact=artifact,
                actual=root_type,
            )
        elif not isinstance(value, expected_type) or (
            str(root_type).strip().casefold() == "number" and isinstance(value, bool)
        ):
            _add_finding(
                findings,
                "artifact_validator_json_root_type_failed",
                "JSON root value does not have the declared type.",
                artifact=artifact,
                expected=str(root_type).strip().casefold(),
                actual=type(value).__name__,
            )
    if required_keys is not None:
        if not isinstance(required_keys, (list, tuple)) or not all(
            isinstance(item, str) and item for item in required_keys
        ):
            _add_finding(
                findings,
                "artifact_validator_specification_invalid",
                "validator.required_json_keys must be a list of non-empty strings.",
                artifact=artifact,
                actual=required_keys,
            )
        elif not isinstance(value, dict):
            _add_finding(
                findings,
                "artifact_validator_required_json_keys_failed",
                "Required JSON keys can only be checked on an object root.",
                artifact=artifact,
                expected=list(required_keys),
                actual=type(value).__name__,
            )
        else:
            missing = [item for item in required_keys if item not in value]
            if missing:
                _add_finding(
                    findings,
                    "artifact_validator_required_json_keys_failed",
                    "JSON artifact omits declared required top-level keys.",
                    artifact=artifact,
                    actual=missing,
                )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _binary_format_error(path: Path, artifact_format: str) -> str | None:
    """Return a deterministic structural error for common binary formats."""

    try:
        size = path.stat().st_size
        with path.open("rb") as source:
            head = source.read(16)
            tail = b""
            if size:
                source.seek(max(0, size - 16))
                tail = source.read(16)
    except OSError as exc:
        return str(exc)
    if size <= 0:
        return "artifact is empty"
    if artifact_format == "binary":
        return None
    if artifact_format == "pdf":
        if not head.startswith(b"%PDF-"):
            return "missing PDF header"
        if b"%%EOF" not in tail:
            return "missing PDF EOF marker"
        return None
    if artifact_format in {"zip", "docx", "xlsx", "pptx"}:
        required_member = {
            "docx": "word/document.xml",
            "xlsx": "xl/workbook.xml",
            "pptx": "ppt/presentation.xml",
        }.get(artifact_format)
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                corrupt = archive.testzip()
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            return f"invalid ZIP container: {exc}"
        if corrupt:
            return f"ZIP member failed CRC validation: {corrupt}"
        if required_member and required_member not in names:
            return f"missing required container member: {required_member}"
        if required_member and "[Content_Types].xml" not in names:
            return "missing required container member: [Content_Types].xml"
        return None
    if artifact_format == "png":
        return None if head.startswith(b"\x89PNG\r\n\x1a\n") else "missing PNG signature"
    if artifact_format == "jpeg":
        return None if head.startswith(b"\xff\xd8") and tail.endswith(b"\xff\xd9") else "invalid JPEG boundary markers"
    if artifact_format == "gif":
        return None if head.startswith((b"GIF87a", b"GIF89a")) else "missing GIF signature"
    if artifact_format == "webp":
        return None if head.startswith(b"RIFF") and head[8:12] == b"WEBP" else "missing WEBP RIFF signature"
    if artifact_format == "parquet":
        return None if head.startswith(b"PAR1") and tail.endswith(b"PAR1") else "invalid Parquet boundary markers"
    return f"unsupported binary verifier: {artifact_format}"


def _workspace_files(
    root: Path,
) -> tuple[
    list[tuple[str, Path]],
    list[str],
    list[dict[str, Any]],
    dict[str, int],
]:
    """Return a complete, bounded workspace snapshot without following links.

    The traversal is deterministic for successful scans.  On an I/O error or
    any budget violation it returns no files/symlinks plus an explicit finding;
    callers must not treat a partial prefix as the workspace contents.
    """

    files: list[tuple[str, Path]] = []
    symlinks: list[str] = []
    findings: list[dict[str, Any]] = []
    scanned_entries = 0
    scanned_path_bytes = 0
    maximum_depth = 0
    # (absolute directory, workspace-relative directory, depth)
    stack: list[tuple[Path, str, int]] = [(root, "", 0)]

    def fail(
        code: str,
        message: str,
        *,
        artifact: Any = None,
        expected: Any = None,
        actual: Any = None,
    ) -> tuple[
        list[tuple[str, Path]],
        list[str],
        list[dict[str, Any]],
        dict[str, int],
    ]:
        _add_finding(
            findings,
            code,
            message,
            artifact=artifact,
            expected=expected,
            actual=actual,
        )
        return [], [], findings, {
            "scanned_entries": scanned_entries,
            "scanned_path_bytes": scanned_path_bytes,
            "maximum_depth": maximum_depth,
            "file_count": 0,
            "symlink_count": 0,
            "complete": 0,
        }

    while stack:
        current, current_relative, current_depth = stack.pop()
        names: list[str] = []
        try:
            # Do not call list(scandir): stop after the first entry beyond the
            # remaining global budget, bounding memory even for one huge dir.
            with os.scandir(current) as entries:
                for entry in entries:
                    scanned_entries += 1
                    if scanned_entries > _MAX_WORKSPACE_SCAN_ENTRIES:
                        return fail(
                            "workspace_scan_entry_limit_exceeded",
                            "Workspace contains more entries than the deterministic verification scan permits.",
                            expected={"maximum_entries": _MAX_WORKSPACE_SCAN_ENTRIES},
                            actual={"observed_at_least": scanned_entries},
                        )
                    names.append(entry.name)
        except OSError as exc:
            return fail(
                "workspace_scan_directory_unreadable",
                "A workspace directory could not be enumerated safely.",
                artifact=current_relative or ".",
                actual=type(exc).__name__,
            )

        child_directories: list[tuple[Path, str, int]] = []
        for name in sorted(names, key=lambda item: (item.casefold(), item)):
            relative = f"{current_relative}/{name}" if current_relative else name
            relative_bytes = len(relative.encode("utf-8", errors="surrogatepass"))
            scanned_path_bytes += relative_bytes
            depth = current_depth + 1
            maximum_depth = max(maximum_depth, depth)
            if relative_bytes > _MAX_WORKSPACE_RELATIVE_PATH_BYTES:
                return fail(
                    "workspace_scan_path_limit_exceeded",
                    "A workspace-relative path exceeds the deterministic verification limit.",
                    artifact=relative,
                    expected={"maximum_path_bytes": _MAX_WORKSPACE_RELATIVE_PATH_BYTES},
                    actual={"path_bytes": relative_bytes},
                )
            if scanned_path_bytes > _MAX_WORKSPACE_SCAN_PATH_BYTES:
                return fail(
                    "workspace_scan_path_budget_exceeded",
                    "Workspace paths exceed the deterministic verification scan budget.",
                    expected={"maximum_total_path_bytes": _MAX_WORKSPACE_SCAN_PATH_BYTES},
                    actual={"observed_path_bytes": scanned_path_bytes},
                )
            if depth > _MAX_WORKSPACE_SCAN_DEPTH:
                return fail(
                    "workspace_scan_depth_limit_exceeded",
                    "Workspace nesting exceeds the deterministic verification scan limit.",
                    artifact=relative,
                    expected={"maximum_depth": _MAX_WORKSPACE_SCAN_DEPTH},
                    actual={"depth": depth},
                )
            if any(
                part in _INTERNAL_WORKSPACE_COMPONENTS
                for part in PurePosixPath(relative).parts
            ):
                continue

            candidate = current / name
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                return fail(
                    "workspace_scan_entry_unreadable",
                    "A workspace entry could not be inspected without following links.",
                    artifact=relative,
                    actual=type(exc).__name__,
                )
            if stat.S_ISLNK(mode):
                symlinks.append(relative)
            elif stat.S_ISDIR(mode):
                child_directories.append((candidate, relative, depth))
            elif stat.S_ISREG(mode):
                files.append((relative, candidate))
            else:
                return fail(
                    "workspace_scan_unsupported_entry_type",
                    "Workspace verification accepts only directories, regular files, and rejected symlinks.",
                    artifact=relative,
                )

        # LIFO with reverse insertion gives lexical depth-first traversal while
        # retaining a bounded stack.  The final sort also makes output order
        # independent of filesystem enumeration order.
        stack.extend(reversed(child_directories))

    files.sort(key=lambda item: (item[0].casefold(), item[0]))
    symlinks.sort(key=lambda item: (item.casefold(), item))
    return files, symlinks, findings, {
        "scanned_entries": scanned_entries,
        "scanned_path_bytes": scanned_path_bytes,
        "maximum_depth": maximum_depth,
        "file_count": len(files),
        "symlink_count": len(symlinks),
        "complete": 1,
    }


def _declaration_pattern(declaration: str) -> str:
    try:
        return normalize_workspace_pattern(declaration).casefold()
    except WorkspacePatternError:
        return ""


def _matches(relative: str, declaration: str) -> bool:
    return workspace_pattern_matches(relative, declaration)


def _matching_files(
    files: list[tuple[str, Path]],
    declaration: str,
) -> list[tuple[str, Path]]:
    return [item for item in files if _matches(item[0], declaration)]


def _verify_artifact_set(
    files: list[tuple[str, Path]],
    declared: list[str],
    contract: Mapping[str, Any],
    policy: Mapping[str, Any],
    findings: list[dict[str, Any]],
    metrics: dict[str, Any],
    *,
    produced_paths: set[str] | None,
) -> None:
    exact = str(policy.get("mode") or "").casefold() == "exact"
    policy_artifacts = _string_list(policy.get("artifacts"))
    set_declarations = policy_artifacts if exact and policy_artifacts else declared
    declared_patterns = [
        pattern for item in set_declarations
        if (pattern := _declaration_pattern(item))
    ]
    allowed = _string_list(policy.get("allowed_additional_patterns"))
    if not allowed:
        allowed = _string_list(contract.get("allowed_additional_patterns"))
    for declaration in allowed:
        try:
            normalize_workspace_pattern(declaration)
        except WorkspacePatternError as exc:
            _add_finding(
                findings,
                "allowed_artifact_pattern_invalid",
                "An allowed-additional artifact pattern is not a safe workspace-relative pattern.",
                artifact=declaration,
                actual=str(exc),
            )
    allowed_patterns = [
        pattern for item in allowed
        if (pattern := _declaration_pattern(item))
    ]
    suffixes = {
        PurePosixPath(pattern).suffix.casefold()
        for pattern in declared_patterns
        if PurePosixPath(pattern).suffix
    }
    # In exact mode every non-internal regular workspace file participates in
    # the set comparison.  A helper script/JSON/text file is still an extra
    # even when the declared deliverables happen to be Markdown-only.
    candidate_files = [
        (relative, path)
        for relative, path in files
        if produced_paths is None or relative.casefold() in produced_paths
    ]
    if exact and produced_paths is not None:
        for declaration in set_declarations:
            current_matches = _matching_files(files, declaration)
            if current_matches and not any(
                relative.casefold() in produced_paths
                for relative, _ in current_matches
            ):
                _add_finding(
                    findings,
                    "declared_artifact_not_produced",
                    "A declared output exists only in the injected pre-run baseline and has no production receipt.",
                    artifact=declaration,
                )
    deliverables = (
        [relative for relative, _ in candidate_files]
        if exact
        else [
            relative
            for relative, _ in candidate_files
            if not suffixes
            or PurePosixPath(relative).suffix.casefold() in suffixes
        ]
    )
    metrics["artifact_set_scope"] = (
        "workspace_snapshot" if produced_paths is None else "run_outputs"
    )
    extras = [
        relative
        for relative in deliverables
        if not any(_matches(relative, declaration) for declaration in set_declarations)
        and not any(
            workspace_pattern_matches(relative, pattern)
            for pattern in allowed_patterns
        )
    ]
    if exact and extras:
        _add_finding(
            findings,
            "unexpected_artifact",
            "The exact artifact-set policy rejects additional deliverable files.",
            actual=extras,
        )

    declared_count = contract.get("declared_file_count")
    if isinstance(declared_count, int) and declared_count > 0:
        counted = [
            relative
            for relative in deliverables
            if (
                any(
                    _matches(relative, declaration)
                    for declaration in set_declarations
                )
                or not any(
                    workspace_pattern_matches(relative, pattern)
                    for pattern in allowed_patterns
                )
            )
        ]
        metrics["declared_file_count"] = declared_count
        metrics["observed_deliverable_count"] = len(counted)
        if len(counted) != declared_count:
            _add_finding(
                findings,
                "artifact_count_mismatch",
                "The workspace deliverable count does not match the Skill declaration.",
                expected=declared_count,
                actual=len(counted),
            )


def _verify_merge(
    modular: list[str],
    final_declaration: str,
    resolved: Mapping[str, Path],
    resolved_relative: Mapping[str, str],
    contract: Mapping[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    merge_required = _merge_is_required(contract)
    # Declaring a final artifact is format-agnostic.  Exact byte concatenation
    # is verified only when the Skill makes merging mandatory, or a legacy
    # package explicitly declares a merge command without the newer flag.
    if not merge_required:
        return {}
    if not final_declaration:
        _add_finding(
            findings,
            "merge_target_undeclared",
            "The contract requires a merge but declares no final artifact.",
        )
        return {"required": True, "byte_equal": False}
    ordered_modular = _ordered_merge_declarations(
        modular,
        resolved_relative,
        contract,
        findings,
    )
    final_path = resolved.get(final_declaration)
    module_paths = [resolved.get(item) for item in ordered_modular]
    if final_path is None or not modular or any(path is None for path in module_paths):
        return {"required": merge_required, "byte_equal": False}

    separator_value = contract.get("merge_separator", "")
    separator = str(separator_value if separator_value is not None else "").encode("utf-8")
    concrete_modules = [path for path in module_paths if isinstance(path, Path)]
    expected_hash = hashlib.sha256()
    expected_bytes = 0
    for index, path in enumerate(concrete_modules):
        if index and separator:
            expected_hash.update(separator)
            expected_bytes += len(separator)
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                expected_hash.update(chunk)
                expected_bytes += len(chunk)

    actual_hash, actual_bytes = _hash_file(final_path)
    byte_equal = _stream_matches_modules(final_path, concrete_modules, separator)
    metrics = {
        "required": merge_required,
        "input_files": [
            resolved_relative.get(item, item) for item in ordered_modular
        ],
        "output_file": resolved_relative.get(final_declaration, final_declaration),
        "expected_sha256": expected_hash.hexdigest(),
        "actual_sha256": actual_hash,
        "expected_bytes": expected_bytes,
        "actual_bytes": actual_bytes,
        "byte_equal": byte_equal,
    }
    if not byte_equal or actual_hash != expected_hash.hexdigest():
        _add_finding(
            findings,
            "merged_content_mismatch",
            "The merged artifact is not the exact current byte concatenation of declared modules.",
            artifact=metrics["output_file"],
            expected={
                "sha256": metrics["expected_sha256"],
                "bytes": expected_bytes,
                "input_files": metrics["input_files"],
            },
            actual={"sha256": actual_hash, "bytes": actual_bytes},
        )
    return metrics


def _ordered_merge_declarations(
    modular: list[str],
    resolved_relative: Mapping[str, str],
    contract: Mapping[str, Any],
    findings: list[dict[str, Any]],
) -> list[str]:
    """Resolve the same explicit merge order enforced by ArtifactPlan."""

    raw_order: Any = None
    for key in ("merge_input_order", "merge_input_files", "merge_inputs"):
        if key in contract:
            raw_order = contract.get(key)
            break
    if raw_order is None:
        nested_orders: list[list[str]] = []
        declarations = contract.get("merge_declarations")
        if isinstance(declarations, list):
            for declaration in declarations:
                if not isinstance(declaration, Mapping):
                    continue
                for key in ("input_order", "input_files", "inputs"):
                    if key not in declaration:
                        continue
                    value = declaration.get(key)
                    if isinstance(value, list) and all(
                        isinstance(item, str) and item
                        for item in value
                    ):
                        nested_orders.append(list(value))
                    break
        unique_orders = list(dict.fromkeys(tuple(item) for item in nested_orders))
        if len(unique_orders) == 1:
            raw_order = list(unique_orders[0])
        elif len(unique_orders) > 1:
            _add_finding(
                findings,
                "merge_input_order_invalid",
                "Merge declarations specify conflicting input orders.",
                actual=nested_orders,
            )
            return modular
    if raw_order is None:
        return modular
    if not isinstance(raw_order, list) or not all(
        isinstance(item, str) and item
        for item in raw_order
    ):
        _add_finding(
            findings,
            "merge_input_order_invalid",
            "Merge input order must be an exact ordered string list.",
            actual=raw_order,
        )
        return modular

    selected: list[str] = []
    used: set[str] = set()
    for item in raw_order:
        matches = [
            declaration
            for declaration in modular
            if declaration.casefold() == item.casefold()
            or (
                declaration in resolved_relative
                and workspace_pattern_matches(
                    resolved_relative[declaration],
                    item,
                )
            )
        ]
        if len(matches) != 1 or matches[0].casefold() in used:
            _add_finding(
                findings,
                "merge_input_order_invalid",
                "Every merge input must match one distinct declared module.",
                artifact=item,
                actual=matches,
            )
            return modular
        selected.append(matches[0])
        used.add(matches[0].casefold())
    if len(selected) != len(modular) or len(used) != len(modular):
        _add_finding(
            findings,
            "merge_input_order_invalid",
            "Merge input order must contain every declared module exactly once.",
            expected=modular,
            actual=raw_order,
        )
        return modular
    return selected


def _stream_matches_modules(
    final_path: Path,
    module_paths: list[Path],
    separator: bytes,
) -> bool:
    with final_path.open("rb") as merged:
        for index, module_path in enumerate(module_paths):
            if index and separator and merged.read(len(separator)) != separator:
                return False
            with module_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    if merged.read(len(chunk)) != chunk:
                        return False
        return merged.read(1) == b""


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _artifact_index_declaration(
    contract: Mapping[str, Any],
    ancillary: list[str],
) -> str:
    index = contract.get("artifact_index")
    if isinstance(index, str):
        return index.strip()
    if isinstance(index, Mapping):
        return _string_value(index.get("file"))
    if contract.get("readme_is_index") is True:
        return "README.md"
    return next(
        (item for item in ancillary if PurePosixPath(item).name.casefold() == "readme.md"),
        "",
    )


def _verify_readme_links(
    root: Path,
    readme_path: Path,
    declared: list[str],
    resolved_relative: Mapping[str, str],
    contract: Mapping[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    relative_readme = readme_path.relative_to(root).as_posix()
    try:
        content = readme_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _add_finding(
            findings,
            "readme_unreadable",
            "The declared artifact index cannot be read.",
            artifact=relative_readme,
            actual=str(exc),
        )
        return

    linked_relatives: set[str] = set()
    for raw_target in _MARKDOWN_LINK_RE.findall(content):
        target = _markdown_link_target(raw_target)
        if (
            not target
            or target.startswith(("#", "//"))
            or _REMOTE_SCHEME_RE.match(target)
        ):
            continue
        target = unquote(target.split("#", 1)[0].split("?", 1)[0]).strip()
        if not target:
            continue
        candidate = (readme_path.parent / target).resolve()
        try:
            linked_relative = candidate.relative_to(root).as_posix()
        except ValueError:
            _add_finding(
                findings,
                "readme_link_outside_workspace",
                "README contains a local link outside the workspace.",
                artifact=relative_readme,
                actual=target,
            )
            continue
        if not candidate.is_file() or candidate.is_symlink():
            _add_finding(
                findings,
                "readme_link_target_missing",
                "README contains a local link whose target does not exist.",
                artifact=relative_readme,
                actual=target,
            )
            continue
        linked_relatives.add(linked_relative.casefold())

    index = contract.get("artifact_index")
    coverage_mode = (
        str(index.get("coverage_mode") or "").casefold()
        if isinstance(index, Mapping)
        else ""
    )
    if coverage_mode == "declared_outputs":
        readme_relative = readme_path.relative_to(root).as_posix().casefold()
        missing_links = [
            relative
            for declaration, relative in resolved_relative.items()
            if declaration in declared
            and relative.casefold() != readme_relative
            and relative.casefold() not in linked_relatives
        ]
        if missing_links:
            _add_finding(
                findings,
                "readme_declared_output_link_missing",
                "README does not link to every declared output artifact.",
                artifact=relative_readme,
                actual=missing_links,
            )


def _markdown_link_target(raw_target: str) -> str:
    target = str(raw_target or "").strip()
    if target.startswith("<") and ">" in target:
        return target[1:target.index(">")].strip()
    return target.split(None, 1)[0].strip() if target else ""


def _checklist_policy(quality: Mapping[str, Any]) -> dict[str, Any]:
    """Compile an explicitly declared quality-checklist policy.

    Merely naming an output ``checklist.*`` is not a quality declaration.  A
    Skill must opt in under ``quality_contract`` so ordinary JSON task boards,
    exports, and inventories are never treated as Markdown report checklists.
    """

    raw = quality.get("checklist")
    if raw is None:
        raw = quality.get("checklist_validation")
    constraints = quality.get("constraints")
    constraints = constraints if isinstance(constraints, Mapping) else {}
    direct_keys = {
        "checklist_file",
        "declared_checklist_rows",
        "checklist_row_mode",
        "require_checklist_status",
        "allow_degraded_checklist",
        "require_merge_receipt",
    }
    enabled = raw is True or isinstance(raw, Mapping) or any(
        key in quality for key in direct_keys
    ) or "declared_checklist_rows" in constraints
    if not enabled or raw is False:
        return {}
    policy = dict(raw) if isinstance(raw, Mapping) else {}
    aliases = {
        "checklist_file": "file",
        "declared_checklist_rows": "rows",
        "checklist_row_mode": "row_mode",
        "require_checklist_status": "require_status",
        "allow_degraded_checklist": "allow_degraded",
        "require_merge_receipt": "require_merge_receipt",
    }
    for source, target in aliases.items():
        if target not in policy and source in quality:
            policy[target] = quality[source]
    if "rows" not in policy and "declared_checklist_rows" in constraints:
        policy["rows"] = constraints.get("declared_checklist_rows")
    policy["enabled"] = True
    return policy


def _checklist_declaration(
    contract: Mapping[str, Any],
    ancillary: list[str],
    policy: Mapping[str, Any],
) -> str:
    if not policy:
        return ""
    explicit = _string_value(policy.get("file"))
    if not explicit:
        explicit = _string_value(contract.get("checklist_file"))
    if explicit:
        return explicit
    return next(
        (item for item in ancillary if "checklist" in PurePosixPath(item).name.casefold()),
        "",
    )


def _verify_checklist(
    checklist_path: Path,
    contract: Mapping[str, Any],
    policy: Mapping[str, Any],
    merge_metrics: Mapping[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    checklist_format = _checklist_format(checklist_path, contract, policy)
    try:
        content = checklist_path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        _add_finding(
            findings,
            "checklist_unreadable",
            "The declared checklist cannot be read.",
            artifact=checklist_path.name,
            actual=str(exc),
        )
        return {"format": checklist_format, "rows": 0, "merge_receipt": False}

    pending_markers = _pending_marker_policy(policy)
    if (
        checklist_format == "markdown"
        and pending_markers is not None
        and _pending_marker_match(content, pending_markers)
    ):
        _add_finding(
            findings,
            "checklist_pending_marker",
            "Checklist contains TODO, an unchecked item, or an explicit failure marker.",
            artifact=checklist_path.name,
        )

    rows, statuses, representations, parse_error = _parse_checklist_rows(
        content,
        checklist_format,
        policy,
    )
    if parse_error:
        _add_finding(
            findings,
            "checklist_format_invalid",
            "The declared checklist could not be parsed in its declared format.",
            artifact=checklist_path.name,
            actual=parse_error,
        )
        return {"format": checklist_format, "rows": 0, "merge_receipt": False}

    require_status = policy.get("require_status", True) is not False
    if require_status and statuses is None:
        _add_finding(
            findings,
            "checklist_status_field_missing",
            "Checklist has no recognizable status field for its declared format.",
            artifact=checklist_path.name,
        )

    expected_rows = policy.get("rows")
    if not isinstance(expected_rows, int) or expected_rows <= 0:
        expected_rows = None
    if isinstance(expected_rows, int):
        row_mode = str(policy.get("row_mode") or "exact").casefold()
        invalid_count = len(rows) < expected_rows if row_mode == "minimum" else len(rows) != expected_rows
        if invalid_count:
            _add_finding(
                findings,
                "checklist_row_count_mismatch",
                "Checklist row count does not satisfy the compiled contract.",
                artifact=checklist_path.name,
                expected={"mode": row_mode, "rows": expected_rows},
                actual=len(rows),
            )

    allow_degraded = policy.get("allow_degraded") is True
    accepted_statuses = _normalized_status_values(policy.get("accepted_statuses"))
    degraded_statuses = _normalized_status_values(policy.get("degraded_statuses"))
    row_statuses: list[str] = []
    for row_number, status in enumerate(statuses or [], start=1):
        classification = _status_classification(
            status,
            accepted_statuses=accepted_statuses,
            degraded_statuses=degraded_statuses,
        )
        row_statuses.append(classification)
        if classification == "pass":
            continue
        if classification == "degraded" and allow_degraded:
            continue
        _add_finding(
            findings,
            "checklist_status_invalid",
            "Every checklist row must carry an explicit accepted completion status.",
            artifact=checklist_path.name,
            expected="PASS/✓" + (" or degraded" if allow_degraded else ""),
            actual={"row": row_number, "status": status},
        )

    merge_receipt = False
    merge_receipt_required = (
        _merge_is_required(contract)
        and policy.get("require_merge_receipt", True) is not False
    )
    if merge_receipt_required:
        merge_rows = [
            (representations[index], row_statuses[index])
            for index in range(min(len(representations), len(row_statuses)))
            if _MERGE_ROW_RE.search(representations[index])
        ]
        if not merge_rows:
            _add_finding(
                findings,
                "checklist_merge_receipt_missing",
                "Mandatory merge has no explicit checklist receipt row.",
                artifact=checklist_path.name,
            )
        else:
            merge_receipt = any(status == "pass" for _, status in merge_rows)
            if not merge_receipt:
                _add_finding(
                    findings,
                    "checklist_merge_receipt_invalid",
                    "Mandatory merge checklist receipt is not marked PASS/✓.",
                    artifact=checklist_path.name,
                )
            if merge_metrics and merge_metrics.get("byte_equal") is not True:
                merge_receipt = False

    return {
        "format": checklist_format,
        "rows": len(rows),
        "expected_rows": expected_rows,
        "merge_receipt": merge_receipt,
    }


def _checklist_format(
    path: Path,
    contract: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> str:
    explicit = policy.get("format")
    if isinstance(explicit, str) and explicit.strip():
        return _normalize_artifact_format(explicit)
    formats = contract.get("artifact_formats")
    if isinstance(formats, Mapping):
        for declaration, raw_format in formats.items():
            if (
                isinstance(declaration, str)
                and isinstance(raw_format, str)
                and PurePosixPath(declaration).name.casefold() == path.name.casefold()
            ):
                return _normalize_artifact_format(raw_format)
    return {
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".csv": "csv",
    }.get(path.suffix.casefold(), "markdown")


def _parse_checklist_rows(
    content: str,
    checklist_format: str,
    policy: Mapping[str, Any],
) -> tuple[list[Any], list[Any] | None, list[str], str | None]:
    if checklist_format == "markdown":
        candidates: list[tuple[list[list[str]], int | None]] = []
        for table in _markdown_tables(content):
            if not table:
                continue
            header = [cell.casefold() for cell in table[0]]
            indexes = [
                index
                for index, cell in enumerate(header)
                if any(token in cell for token in _STATUS_HEADERS)
            ]
            candidates.append((table[1:], indexes[0] if indexes else None))
        if not candidates:
            return [], None, [], "no Markdown checklist table"
        rows, status_index = max(candidates, key=lambda item: len(item[0]))
        statuses = (
            [row[status_index] if status_index < len(row) else "" for row in rows]
            if status_index is not None
            else None
        )
        return rows, statuses, [" | ".join(row) for row in rows], None

    if checklist_format == "csv":
        try:
            reader = csv.DictReader(io.StringIO(content), strict=True)
            rows = list(reader)
        except csv.Error as exc:
            return [], None, [], str(exc)
        if reader.fieldnames is None:
            return [], None, [], "CSV checklist has no header"
        statuses = _structured_checklist_statuses(rows, policy)
        return rows, statuses, [json.dumps(row, sort_keys=True) for row in rows], None

    if checklist_format in {"json", "yaml"}:
        try:
            value = json.loads(content) if checklist_format == "json" else yaml.safe_load(content)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            return [], None, [], str(exc)
        rows = _structured_checklist_rows(value, policy)
        if rows is None:
            return [], None, [], "checklist root has no declared row collection"
        statuses = _structured_checklist_statuses(rows, policy)
        return (
            rows,
            statuses,
            [json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) for row in rows],
            None,
        )
    return [], None, [], f"unsupported checklist format: {checklist_format}"


def _structured_checklist_rows(
    value: Any,
    policy: Mapping[str, Any],
) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if not isinstance(value, Mapping):
        return None
    rows_field = policy.get("rows_field")
    candidates = [rows_field] if isinstance(rows_field, str) and rows_field else []
    candidates.extend(["rows", "items", "entries", "tasks", "checklist", "checks"])
    for key in candidates:
        rows = value.get(key)
        if isinstance(rows, list):
            return rows
    return None


def _structured_checklist_statuses(
    rows: list[Any],
    policy: Mapping[str, Any],
) -> list[Any] | None:
    field = policy.get("status_field")
    field_names = [field] if isinstance(field, str) and field else []
    field_names.extend(["status", "state", "result", "completed", "complete", "done"])
    selected = next(
        (
            key
            for key in field_names
            if any(isinstance(row, Mapping) and key in row for row in rows)
        ),
        None,
    )
    if selected is None:
        return None
    return [row.get(selected) if isinstance(row, Mapping) else None for row in rows]


def _normalized_status_values(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple)):
        return set()
    return {
        str(item).strip().casefold()
        for item in value[:64]
        if isinstance(item, (str, int, float)) and str(item).strip()
    }


def _status_classification(
    value: Any,
    *,
    accepted_statuses: set[str] | None = None,
    degraded_statuses: set[str] | None = None,
) -> str:
    if value is True:
        return "pass"
    if value is False or value is None:
        return "fail"
    text = re.sub(r"[*_`]", "", str(value or "")).strip()
    folded = text.casefold()
    if accepted_statuses and folded in accepted_statuses:
        return "pass"
    if degraded_statuses and folded in degraded_statuses:
        return "degraded"
    if not text or _STATUS_FAILURE_RE.search(text):
        return "fail"
    if _STATUS_DEGRADED_RE.search(text):
        return "degraded"
    if _STATUS_PASS_RE.search(text):
        return "pass"
    return "unknown"


def _markdown_tables(content: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    block: list[list[str]] = []
    for line in content.splitlines() + [""]:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", cell or "") for cell in cells):
                continue
            block.append(cells)
            continue
        if block:
            tables.append(block)
            block = []
    return tables


def _pending_marker_policy(
    quality: Mapping[str, Any],
) -> tuple[str, ...] | None:
    raw = quality.get("pending_markers")
    if raw is None and quality.get("forbid_pending_markers") is True:
        raw = True
    if raw is True:
        return ()
    if isinstance(raw, (list, tuple)) and all(
        isinstance(item, str) and item for item in raw
    ):
        return tuple(raw[:64])
    if isinstance(raw, Mapping) and raw.get("enabled") is True:
        patterns = raw.get("patterns")
        if isinstance(patterns, (list, tuple)) and all(
            isinstance(item, str) and item for item in patterns
        ):
            return tuple(patterns[:64])
        return ()
    return None


def _pending_marker_match(content: str, markers: tuple[str, ...]) -> bool:
    if not markers:
        return bool(_PENDING_RE.search(content))
    folded = content.casefold()
    return any(marker.casefold() in folded for marker in markers)


def _is_plain_text_artifact(path: Path, contract: Mapping[str, Any]) -> bool:
    """Return whether report-text heuristics are meaningful for this artifact."""

    formats = contract.get("artifact_formats")
    if isinstance(formats, Mapping):
        for declaration, raw_format in formats.items():
            if (
                isinstance(declaration, str)
                and isinstance(raw_format, str)
                and PurePosixPath(declaration).name.casefold() == path.name.casefold()
            ):
                return _normalize_artifact_format(raw_format) in {
                    "markdown", "text", "html",
                }
    suffix = path.suffix.casefold()
    if suffix in {".json", ".yaml", ".yml", ".csv", ".svg", ".xml"}:
        return False
    return suffix in {".md", ".markdown", ".txt", ".rst", ".html", ".htm"}


def _padding_checks_enabled(quality: Mapping[str, Any]) -> bool:
    raw = quality.get("padding_policy")
    if isinstance(raw, Mapping):
        return raw.get("enabled") is True
    return quality.get("detect_padding") is True


def _verify_nonempty_artifact(
    path: Path,
    root: Path,
    findings: list[dict[str, Any]],
) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    empty = size <= 0
    if not empty and path.suffix.casefold() in {
        ".md", ".markdown", ".txt", ".rst", ".html", ".htm",
        ".json", ".yaml", ".yml", ".csv", ".svg", ".xml",
    }:
        try:
            empty = not path.read_text(encoding="utf-8", errors="strict").strip()
        except (OSError, UnicodeError):
            empty = False
    if empty:
        _add_finding(
            findings,
            "empty_artifact",
            "A declared artifact is empty.",
            artifact=path.relative_to(root).as_posix(),
        )


def _verify_pending_markers(
    path: Path,
    root: Path,
    findings: list[dict[str, Any]],
    *,
    markers: tuple[str, ...],
) -> None:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if _pending_marker_match(content, markers):
        _add_finding(
            findings,
            "pending_completion_marker",
            "A declared artifact contains TODO, an unchecked item, or an explicit failure marker.",
            artifact=path.relative_to(root).as_posix(),
        )


def _verify_minimum_padding(
    path: Path,
    root: Path,
    findings: list[dict[str, Any]],
) -> None:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    artifact = path.relative_to(root).as_posix()
    if not content.strip():
        _add_finding(
            findings,
            "empty_artifact",
            "A declared content artifact is empty or contains only whitespace.",
            artifact=artifact,
        )
        return
    if len(content) >= 1_000:
        whitespace_ratio = sum(char.isspace() for char in content) / max(1, len(content))
        if whitespace_ratio > 0.65:
            _add_finding(
                findings,
                "excessive_whitespace_padding",
                "Artifact contains a suspicious proportion of whitespace padding.",
                artifact=artifact,
                expected="whitespace ratio <= 0.65",
                actual=round(whitespace_ratio, 4),
            )

    lines = content.splitlines()
    if len(lines) >= 20:
        blank_count = sum(1 for line in lines if not line.strip())
        blank_ratio = blank_count / len(lines)
        max_blank_run = _max_blank_run(lines)
        if blank_ratio > 0.60 or max_blank_run > 20:
            _add_finding(
                findings,
                "excessive_blank_line_padding",
                "Artifact contains suspicious blank-line padding.",
                artifact=artifact,
                expected={"blank_ratio_max": 0.60, "consecutive_blank_max": 20},
                actual={
                    "blank_ratio": round(blank_ratio, 4),
                    "max_consecutive_blank": max_blank_run,
                },
            )

    paragraphs = [
        _normalize_repetition_unit(paragraph)
        for paragraph in re.split(r"\n\s*\n", content)
    ]
    paragraphs = [paragraph for paragraph in paragraphs if len(paragraph) >= 80]
    if len(paragraphs) >= 4:
        counts = Counter(paragraphs)
        duplicate_occurrences = sum(count - 1 for count in counts.values() if count > 1)
        if max(counts.values(), default=0) >= 4 or duplicate_occurrences / len(paragraphs) > 0.30:
            _add_finding(
                findings,
                "repeated_content_padding",
                "Artifact repeats substantive paragraphs often enough to resemble padding.",
                artifact=artifact,
                expected="no paragraph repeated 4+ times and duplicate ratio <= 0.30",
                actual={
                    "paragraphs": len(paragraphs),
                    "duplicate_occurrences": duplicate_occurrences,
                    "max_repetition": max(counts.values(), default=0),
                },
            )

    substantive_lines = [
        _normalize_repetition_unit(line)
        for line in lines
        if len(_normalize_repetition_unit(line)) >= 32
        and not line.lstrip().startswith(("#", "|", "```", "---"))
    ]
    if len(substantive_lines) >= 30:
        unique_ratio = len(set(substantive_lines)) / len(substantive_lines)
        if unique_ratio < 0.45:
            _add_finding(
                findings,
                "low_unique_content_ratio",
                "Artifact has too few unique substantive lines for its length.",
                artifact=artifact,
                expected="unique substantive-line ratio >= 0.45",
                actual=round(unique_ratio, 4),
            )


def _max_blank_run(lines: list[str]) -> int:
    longest = 0
    current = 0
    for line in lines:
        if line.strip():
            current = 0
            continue
        current += 1
        longest = max(longest, current)
    return longest


def _normalize_repetition_unit(value: str) -> str:
    value = re.sub(r"<!--.*?-->", "", str(value or ""), flags=re.DOTALL)
    value = re.sub(r"[*_`>#]", "", value)
    value = re.sub(r"^\s*(?:[-+] |\d+[.)]\s*)", "", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _string_value(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


def _add_finding(
    findings: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    artifact: Any = None,
    expected: Any = None,
    actual: Any = None,
) -> None:
    finding = {"code": code, "severity": "error", "message": message}
    if artifact not in (None, "", [], {}):
        finding["artifact"] = artifact
    if expected not in (None, "", [], {}):
        finding["expected"] = expected
    if actual not in (None, "", [], {}):
        finding["actual"] = actual
    findings.append(finding)


__all__ = ["verify_artifact_contract"]
