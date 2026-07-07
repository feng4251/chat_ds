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

    result: Dict[str, Any] = {
        "name": name,
        "description": description,
        "content": content,
        "tags": tags,
        "related_skills": related_skills,
        "linked_files": linked_files if linked_files else None,
        "resource_graph": resource_graph if resource_graph else None,
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
