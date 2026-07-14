"""Skill loader — YAML frontmatter parsing and template variable substitution.

Simplified from hermes-agent/agent/skill_utils.py and skill_preprocessing.py:
- No YAML CSafeLoader (PyYAML not guaranteed available; uses simple regex fallback)
- No inline-shell preprocessing
- Template vars: ${SKILL_DIR}, ${SESSION_ID} (renamed from HERMES_* prefix)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

# Matches ${SKILL_DIR} / ${SESSION_ID} tokens in SKILL.md content.
_TEMPLATE_RE = re.compile(r"\$\{(SKILL_DIR|SESSION_ID)\}")

# Sentinel for yaml availability
_yaml_load_fn = None


def _get_yaml_loader():
    """Lazy-import YAML loader with SafeLoader preference."""
    global _yaml_load_fn
    if _yaml_load_fn is None:
        try:
            import yaml
            loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader

            def _load(value: str):
                return yaml.load(value, Loader=loader)

            _yaml_load_fn = _load
        except ImportError:
            _yaml_load_fn = False  # type: ignore[assignment]
    return _yaml_load_fn


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    Uses PyYAML if available, with a fallback to simple key:value splitting.

    Returns:
        (frontmatter_dict, remaining_body)
    """
    frontmatter: Dict[str, Any] = {}
    body = content

    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3:end_match.start() + 3]
    body = content[end_match.end() + 3:]

    loader = _get_yaml_loader()
    if loader:
        try:
            parsed = loader(yaml_content)
            if isinstance(parsed, dict):
                frontmatter = parsed
        except Exception:
            # Fall through to simple parser
            pass
    if not frontmatter:
        # Simple key:value fallback
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


def substitute_template_vars(
    content: str,
    skill_dir: str | None = None,
    session_id: str | None = None,
) -> str:
    """Replace ${SKILL_DIR} and ${SESSION_ID} tokens in skill content.

    Only substitutes tokens for which a concrete value is available.
    Unresolved tokens are left in place.
    """
    if not content or "${" not in content:
        return content

    def _replace(match: re.Match) -> str:
        token = match.group(1)
        if token == "SKILL_DIR" and skill_dir:
            return skill_dir
        if token == "SESSION_ID" and session_id:
            return str(session_id)
        return match.group(0)

    return _TEMPLATE_RE.sub(_replace, content)


def load_skill_content(
    skill_path: Path,
    skill_dir: str | None = None,
    session_id: str | None = None,
) -> Dict[str, Any]:
    """Load and parse a SKILL.md file, returning structured data.

    Args:
        skill_path: Path to the SKILL.md file.
        skill_dir: Absolute path to the skill's directory (for template substitution).
        session_id: Session identifier (for ${SESSION_ID} substitution).

    Returns:
        Dict with keys: name, description, content (processed), tags,
        related_skills, linked_files, frontmatter (raw).
    """
    try:
        raw = skill_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        return {"error": f"Cannot read skill file: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error reading skill: {e}"}

    frontmatter, body = parse_frontmatter(raw)

    # Apply template variable substitution
    skill_dir_path = Path(skill_dir) if skill_dir else skill_path.parent
    content = substitute_template_vars(
        body,
        skill_dir=str(skill_dir_path),
        session_id=session_id,
    )

    name = str(frontmatter.get("name") or skill_path.parent.name)[:64]
    description = str(frontmatter.get("description", ""))

    # Extract tags and related_skills from metadata.hermes or top-level
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    hermes_meta = metadata.get("hermes") or {}
    if not isinstance(hermes_meta, dict):
        hermes_meta = {}

    tags = _parse_tags(hermes_meta.get("tags") or frontmatter.get("tags", ""))
    related_skills = _parse_tags(
        hermes_meta.get("related_skills") or frontmatter.get("related_skills", "")
    )

    # Discover linked files and workflow resources
    linked_files = _discover_linked_files(skill_dir_path)
    resource_graph = _discover_resource_graph(skill_dir_path, linked_files)
    workflow_contract = _discover_workflow_contract(skill_dir_path, linked_files, content)

    result: Dict[str, Any] = {
        "name": name,
        "description": description,
        "content": content,
        "tags": tags,
        "related_skills": related_skills,
        "linked_files": linked_files if linked_files else None,
        "resource_graph": resource_graph if resource_graph else None,
        "workflow_contract": workflow_contract if workflow_contract else None,
        "frontmatter": frontmatter,
    }

    # Surface agentskills.io optional fields
    if frontmatter.get("version"):
        result["version"] = frontmatter["version"]
    if frontmatter.get("license"):
        result["license"] = frontmatter["license"]
    if frontmatter.get("compatibility"):
        result["compatibility"] = frontmatter["compatibility"]

    # ── MCP server dependencies ──────────────────────────────────────────
    mcp_servers = frontmatter.get("mcp_servers")
    if mcp_servers and isinstance(mcp_servers, list):
        # Apply template substitution to each MCP server config entry
        resolved_mcp = []
        for entry in mcp_servers:
            if isinstance(entry, dict):
                resolved_entry = {}
                for k, v in entry.items():
                    if isinstance(v, str):
                        resolved_entry[k] = substitute_template_vars(
                            v, skill_dir=str(skill_dir_path), session_id=session_id,
                        )
                    elif isinstance(v, list):
                        resolved_entry[k] = [
                            substitute_template_vars(
                                item, skill_dir=str(skill_dir_path), session_id=session_id,
                            ) if isinstance(item, str) else item
                            for item in v
                        ]
                    else:
                        resolved_entry[k] = v
                resolved_mcp.append(resolved_entry)
        result["mcp_servers"] = resolved_mcp
        result["mcp_config_hint"] = (
            "This skill declares MCP dependencies. Registration is owned by "
            "the harness control plane and should already have happened during "
            "installation. Use mcp_server_status to inspect failures; do not "
            "manually remove or recreate the server from the model."
        )

    return result


def _parse_tags(tags_value) -> list[str]:
    """Parse tags from a frontmatter value.

    Handles lists (from YAML), bracket-wrapped strings, and comma-separated strings.
    """
    if not tags_value:
        return []
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]
    tags_str = str(tags_value).strip()
    if tags_str.startswith("[") and tags_str.endswith("]"):
        tags_str = tags_str[1:-1]
    return [t.strip().strip("'\"") for t in tags_str.split(",") if t.strip()]


_WORKFLOW_DIRS = (
    "orchestration",
    "workers",
    "workflows",
    "references",
    "templates",
    "formats",
    "protocols",
    "scripts",
    "examples",
    "evaluation",
    "assets",
)

_TEXT_RESOURCE_SUFFIXES = {
    ".md", ".txt", ".rst",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
    ".py", ".sh", ".bash", ".js", ".ts", ".tsx", ".jsx",
    ".sql", ".csv", ".r", ".jl",
}


def _discover_linked_files(skill_dir: Path) -> dict[str, list[str]]:
    """Discover linked reference, workflow, template, asset, and script files."""
    linked: dict[str, list[str]] = {}

    for directory in _WORKFLOW_DIRS:
        subdir = skill_dir / directory
        if not subdir.is_dir():
            continue
        files = _list_resource_files(skill_dir, subdir)
        if files:
            linked[directory] = files

    # Common domain-specific resource directories should be surfaced without
    # knowing their semantics in advance.
    for subdir in sorted(p for p in skill_dir.iterdir() if p.is_dir()):
        if subdir.name.startswith(".") or subdir.name in linked:
            continue
        if subdir.name in {"__pycache__", "node_modules", ".git"}:
            continue
        files = _list_resource_files(skill_dir, subdir, limit=50)
        if files:
            linked[subdir.name] = files

    # openclaw-compatible .mcp.json (MCP server configuration)
    mcp_json = skill_dir / ".mcp.json"
    if mcp_json.is_file():
        linked["mcp_config"] = [".mcp.json"]

    root_files = []
    for child in sorted(skill_dir.iterdir()):
        if not child.is_file() or child.name == "SKILL.md":
            continue
        if child.name.startswith(".") and child.name != ".mcp.json":
            continue
        if child.suffix.lower() in _TEXT_RESOURCE_SUFFIXES or child.name.startswith("requirements"):
            root_files.append(str(child.relative_to(skill_dir)))
    if root_files:
        linked["root_files"] = root_files

    return linked


def _list_resource_files(skill_dir: Path, directory: Path, limit: int = 200) -> list[str]:
    files: list[str] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if any(part in {"__pycache__", "node_modules", ".git"} for part in path.parts):
            continue
        if path.suffix.lower() not in _TEXT_RESOURCE_SUFFIXES:
            continue
        files.append(str(path.relative_to(skill_dir)))
        if len(files) >= limit:
            break
    return files


def _discover_resource_graph(
    skill_dir: Path,
    linked_files: dict[str, list[str]],
) -> dict[str, Any]:
    """Return a compact, generic resource graph for progressive disclosure."""
    if not linked_files:
        return {}

    important_categories = [
        name for name in (
            "orchestration", "workers", "workflows", "protocols", "formats",
            "references", "scripts", "evaluation", "examples", "templates",
            "therapeutic-areas", "domains", "rwe",
        )
        if name in linked_files
    ]
    categories = {
        name: {
            "count": len(files),
            "sample": files[:12],
        }
        for name, files in linked_files.items()
    }
    suggested_files: list[str] = []
    for category in important_categories:
        suggested_files.extend(linked_files.get(category, [])[:8])
    return {
        "skill_root": str(skill_dir),
        "categories": categories,
        "important_categories": important_categories,
        "suggested_files": suggested_files[:40],
        "hint": (
            "For complex tasks, inspect relevant suggested_files with "
            "skill_view(name, file_path=...) before drafting the final artifact."
        ),
    }


def _parse_bytes_literal(value: Any) -> int | None:
    """Parse a size literal such as '150KB', '100 KB', '2MB', or '102400' into bytes."""
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(kb|mb|gb|k|m|g|b)?", value.strip(), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    factor = {"b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3}
    return int(number * factor.get(unit, 1))


def _parse_int_literal(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d[\d,]*", value)
        if match:
            return int(match.group(0).replace(",", ""))
    return None


def _parse_orchestrator_contract(skill_dir: Path) -> dict[str, Any]:
    """Parse structured YAML orchestrator contracts under orchestration/ and workflows/.

    Reads the skill's OWN declared final_report_template (sections + auto_merge) so the
    harness executes the skill's intent instead of inferring it from prose. Returns an
    empty dict when no orchestrator YAML declares a final report template.
    """
    loader = _get_yaml_loader()
    if not loader:
        return {}
    candidates: list[Path] = []
    for sub in ("orchestration", "workflows", "orchestrator"):
        base = skill_dir / sub
        if base.is_dir():
            candidates.extend(sorted(p for p in base.glob("*.y*ml") if p.is_file()))
    candidates.extend(sorted(p for p in skill_dir.glob("orchestrat*.y*ml") if p.is_file()))

    contract: dict[str, Any] = {}
    for path in _dedupe_paths_local(candidates):
        text = _read_text_resource(path)
        if not text:
            continue
        try:
            data = loader(text)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        template = data.get("final_report_template")
        if not isinstance(template, dict):
            continue

        sections = template.get("sections")
        if isinstance(sections, list):
            declared_sections = [s for s in sections if isinstance(s, dict) and s.get("section")]
            if declared_sections:
                contract["declared_section_count"] = len(declared_sections)
                contract["section_titles"] = [str(s.get("section")) for s in declared_sections][:60]

        auto_merge = template.get("auto_merge")
        if isinstance(auto_merge, dict):
            output_artifact = auto_merge.get("output_artifact")
            command = auto_merge.get("command_template") or auto_merge.get("command")
            if output_artifact or command:
                contract["merge_mandatory"] = bool(auto_merge.get("mandatory"))
                if output_artifact:
                    contract["declared_final_artifact"] = str(output_artifact).strip()
                if command:
                    contract["merge_command"] = str(command).strip()
                size_range = auto_merge.get("expected_size_range")
                if isinstance(size_range, str) and "-" in size_range:
                    low, _, high = size_range.partition("-")
                    low_bytes = _parse_bytes_literal(low)
                    high_bytes = _parse_bytes_literal(high)
                    if low_bytes:
                        contract["expected_min_bytes"] = low_bytes
                    if high_bytes:
                        contract["expected_max_bytes"] = high_bytes
                checks = auto_merge.get("post_merge_verification")
                if isinstance(checks, list):
                    parsed_checks = [str(c).strip() for c in checks if str(c).strip()]
                    if parsed_checks:
                        contract["post_merge_checks"] = parsed_checks[:20]
                        for check in parsed_checks:
                            min_lines = re.search(r"line count\s*>\s*([\d,]+)|>\s*([\d,]+)\s*lines", check, re.IGNORECASE)
                            if min_lines:
                                value = _parse_int_literal(min_lines.group(1) or min_lines.group(2))
                                if value:
                                    contract["expected_min_lines"] = value
                            min_bytes = re.search(r"size\s*>\s*([\d.]+\s*[kmg]?b)|>\s*([\d.]+\s*[kmg]b)", check, re.IGNORECASE)
                            if min_bytes and "expected_min_bytes" not in contract:
                                value = _parse_bytes_literal(min_bytes.group(1) or min_bytes.group(2))
                                if value:
                                    contract["expected_min_bytes"] = value
    return contract


def _parse_output_format_contract(skill_dir: Path) -> dict[str, Any]:
    """Parse formats/*.md output-packaging specs (declared file count, modular files).

    The output format spec is the skill's declaration of HOW the report is packaged
    (e.g. '11 content files + README + _checklist + FULL_REPORT = 14 total'). Returns
    an empty dict when no format spec is present.
    """
    formats_dir = skill_dir / "formats"
    if not formats_dir.is_dir():
        return {}
    contract: dict[str, Any] = {}
    modular_files: list[str] = []
    output_format_files: list[str] = []
    for path in sorted(p for p in formats_dir.glob("*.md") if p.is_file()):
        text = _read_text_resource(path)
        if not text:
            continue
        declares_output_packaging = False
        # Declared total file count, e.g. "= 14 total" or "File count | 11 content files ... = 14 total"
        total_match = re.search(r"=\s*(\d+)\s*total", text, re.IGNORECASE)
        if total_match:
            declares_output_packaging = True
            if "declared_file_count" not in contract:
                contract["declared_file_count"] = int(total_match.group(1))
        # Modular numbered output files, e.g. `01_executive_summary.md`
        for match in re.finditer(r"`?(\d{2}_[A-Za-z0-9_.-]+\.md)`?", text):
            declares_output_packaging = True
            modular_files.append(match.group(1))
        if declares_output_packaging:
            output_format_files.append(str(path.relative_to(skill_dir)))
    if modular_files:
        contract["declared_modular_files"] = _dedupe(modular_files)[:40]
    if output_format_files:
        contract["output_format_files"] = _dedupe(output_format_files)[:20]
    return contract


def _dedupe_paths_local(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _discover_workflow_contract(
    skill_dir: Path,
    linked_files: dict[str, list[str]],
    skill_body: str,
) -> dict[str, Any]:
    files = _flatten_linked_files(linked_files)
    workflow_files = [
        path for path in files
        if path.startswith(("orchestration/", "workflows/", "workers/", "protocols/"))
    ]
    worker_files = [
        path for path in files
        if "/workers/" in path or path.startswith("workers/") or _basename(path).startswith("worker-")
    ]
    format_files = [path for path in files if path.startswith("formats/") or "/formats/" in path]
    script_files = [path for path in files if path.startswith("scripts/") or path.endswith(".py") or path.endswith(".sh")]

    artifact_patterns: list[str] = []
    merge_requirements: list[str] = []
    sanity_checks: list[str] = []
    declared_external_sources: list[str] = []
    scanned_files = _dedupe(files[:80])
    scanned_files.insert(0, "SKILL.md")
    scanned_files = _dedupe(scanned_files)
    for rel_path in scanned_files:
        text = skill_body if rel_path == "SKILL.md" else _read_text_resource(skill_dir / rel_path)
        if not text:
            continue
        artifact_patterns.extend(_extract_artifact_patterns(text))
        merge_requirements.extend(_extract_merge_requirements(text))
        sanity_checks.extend(_extract_sanity_checks(text))
        declared_external_sources.extend(_extract_external_sources(text))

    workers = [
        {
            "id": _worker_id_from_path(path),
            "file": path,
            "role_hint": _worker_role_hint(skill_dir / path),
        }
        for path in worker_files
    ]

    # Structured contract from the skill's OWN declarations (authoritative).
    output_contract: dict[str, Any] = {}
    output_contract.update(_parse_orchestrator_contract(skill_dir))
    for key, value in _parse_output_format_contract(skill_dir).items():
        output_contract.setdefault(key, value)
    output_format_files = [
        str(path) for path in output_contract.get("output_format_files") or []
        if isinstance(path, str)
    ]

    # requires_merge is authoritative: true ONLY when the skill declares a final
    # merged artifact / merge command (not merely because prose mentions "full report").
    declares_merge = bool(
        output_contract.get("declared_final_artifact")
        or output_contract.get("merge_command")
    )
    declares_modular = bool(output_contract.get("declared_modular_files"))

    contract: dict[str, Any] = {
        "workflow_files": _dedupe(workflow_files)[:80],
        "orchestrator_files": [path for path in workflow_files if "orchestrator" in _basename(path).lower()][:20],
        "worker_files": _dedupe(worker_files)[:80],
        "workers": workers[:80],
        "format_files": output_format_files[:40],
        "script_candidates": _dedupe(script_files)[:40],
        "artifact_patterns": _dedupe(artifact_patterns)[:80],
        "merge_requirements": _dedupe(merge_requirements)[:40],
        "sanity_checks": _dedupe(sanity_checks)[:60],
        "declared_external_sources": _dedupe(declared_external_sources)[:40],
        "output_contract": output_contract,
        "requires_worker_outputs": bool(worker_files),
        "requires_modular_artifacts": declares_modular or bool(artifact_patterns),
        "requires_merge": declares_merge,
        "recommended_execution": [
            "Load __manifest__, then inspect orchestrator/workflow files first.",
            "Inspect each declared worker file and collect evidence for every worker before final synthesis.",
            "Generate the declared modular artifacts/checklist in the session workspace when artifact patterns are specified.",
            "Use run_skill_python for skill-provided Python entrypoints; use execute_code only for small ad-hoc calculations.",
            "Run the declared merge/sanity steps and verify the final artifact before stopping.",
        ],
    }
    return {key: value for key, value in contract.items() if value not in ([], {}, None, False)}


def _flatten_linked_files(linked_files: dict[str, list[str]]) -> list[str]:
    result: list[str] = []
    for files in linked_files.values():
        result.extend(str(path) for path in files if isinstance(path, str))
    return _dedupe(result)


def _read_text_resource(path: Path, limit: int = 120_000) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _TEXT_RESOURCE_SUFFIXES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _basename(path: str) -> str:
    return str(path).rsplit("/", 1)[-1]


def _worker_id_from_path(path: str) -> str:
    name = _basename(path)
    stem = name.rsplit(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-") or stem


def _worker_role_hint(path: Path) -> str:
    text = _read_text_resource(path, limit=8_000)
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip().strip("#")
        if not stripped or len(stripped) > 180:
            continue
        lowered = stripped.lower()
        if any(key in lowered for key in ("role", "worker", "agent", "objective", "mission", "task")):
            return stripped[:180]
    return ""


def _extract_artifact_patterns(text: str) -> list[str]:
    patterns: list[str] = []
    for match in re.finditer(r"(?:`|\b)([A-Za-z0-9_{}*?.-]+\.(?:md|json|csv|tsv|txt|yaml|yml))(?:`|\b)", text):
        token = match.group(1)
        if any(marker in token.lower() for marker in ("readme.md", "checklist", "report", "full", "output", "result")) or "*" in token or "{" in token:
            patterns.append(token)
    for match in re.finditer(r"\b(\d{2}_[A-Za-z0-9_*?.-]+\.md)\b", text):
        patterns.append(match.group(1))
    file_count = re.search(r"(\d+)\s+(?:content\s+)?files?", text, re.IGNORECASE)
    if file_count:
        patterns.append(f"declared_file_count:{file_count.group(1)}")
    return patterns


def _extract_merge_requirements(text: str) -> list[str]:
    requirements: list[str] = []
    lowered = text.lower()
    if "auto-merge" in lowered or "auto merge" in lowered or "full_report" in lowered or "full report" in lowered:
        requirements.append("auto_merge_full_report")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered_line = stripped.lower()
        if ("cat " in lowered_line and ">" in lowered_line) or "merge" in lowered_line and "report" in lowered_line:
            requirements.append(stripped[:240])
    return requirements


def _extract_sanity_checks(text: str) -> list[str]:
    checks: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().strip("-* ")
        lowered = stripped.lower()
        if not stripped or len(stripped) > 240:
            continue
        if any(key in lowered for key in ("sanity", "checklist", "file size", "line count", "must", "required", "> 100", ">100", "verify")):
            checks.append(stripped)
    return checks[:80]


def _extract_external_sources(text: str) -> list[str]:
    sources = []
    known = (
        "PubMed", "ClinicalTrials", "ClinicalTrials.gov", "FDA", "EMA", "WHO",
        "UniProt", "NCBI", "Gene", "MeSH", "DrugBank", "ChEMBL", "OpenAlex",
        "Crossref", "Semantic Scholar", "Europe PMC", "SEC", "USPTO",
    )
    for source in known:
        if re.search(rf"\b{re.escape(source)}\b", text, re.IGNORECASE):
            sources.append(source)
    return sources


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
