"""Skill loader — YAML frontmatter parsing and scoped template substitution.

Simplified from hermes-agent/agent/skill_utils.py and skill_preprocessing.py:
- SafeLoader/CSafeLoader when available; a restricted scalar fallback otherwise
- No inline-shell preprocessing
- Template vars in runtime configuration, plus explicitly versioned body templates
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

from retrieval_policy import (
    RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
    RETRIEVAL_COMPLETENESS_POLICY_EXHAUSTIVE,
    normalize_retrieval_completeness_policy,
)

from skills.path_safety import (
    iter_safe_regular_files,
    validate_skill_resource,
    validate_skill_root,
)
from skills.route_safety import (
    MAX_ROUTE_PATTERN_CHARS,
    MAX_ROUTE_PATTERNS_PER_ROUTE,
    MAX_ROUTE_PATTERNS_TOTAL,
    MAX_SKILL_ROUTES,
    route_pattern_validation_error,
)
from skills.command_grants import (
    compile_environment_command_grants,
    parse_allowed_tool_selectors,
)

logger = logging.getLogger(__name__)

# Matches ${SKILL_DIR} / ${SESSION_ID} tokens in explicitly templated values.
_TEMPLATE_RE = re.compile(r"\$\{(SKILL_DIR|SESSION_ID)\}")

# Sentinel for yaml availability
_yaml_load_fn = None


class FrontmatterParseError(ValueError):
    """Stable parse failure used by package compilation diagnostics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = dict(context or {})


# Declarative Skill YAML is untrusted package data.  These bounds are high
# enough for large real-world orchestration contracts, but low enough that an
# alias bomb, accidental recursive alias, or generated schema cannot make the
# compiler recurse forever or build an unbounded execution prompt.
MAX_COMPILER_STRUCTURE_DEPTH = 64
MAX_COMPILER_STRUCTURE_NODES = 20_000
MAX_COMPILER_SCALAR_CHARS = 500_000
MAX_COMPILER_YAML_SOURCE_CHARS = 1_000_000
MAX_COMPACT_STRUCTURE_DEPTH = 48
MAX_COMPACT_STRUCTURE_NODES = 10_000
MAX_COMPACT_SCALAR_CHARS = 250_000
MAX_COMPILED_WORKERS = 80
MAX_DECLARED_LOCAL_RESOURCES = 512
# Legacy prose discovery is intentionally secondary to the structured Skill
# compiler, but it still influences artifact/merge hints. Never infer a
# contract from an arbitrary prefix. Oversized authoritative workflow prose is
# invalid; supporting/reference prose remains addressable in the resource
# graph but is excluded from contract inference unless it has blocking
# resource authority.
MAX_WORKFLOW_SEMANTIC_SCAN_FILES = 512
MAX_WORKFLOW_SEMANTIC_FILE_CHARS = 500_000
MAX_WORKFLOW_SEMANTIC_TOTAL_CHARS = 16_000_000
MAX_ENVIRONMENT_CONTRACT_ITEMS = 256
MAX_SKILL_FRONTMATTER_CHARS = MAX_COMPILER_YAML_SOURCE_CHARS
MAX_EXTERNAL_SOURCE_SCAN_CHARS = 500_000
MAX_EXTERNAL_SOURCE_COUNT = 128
MAX_EXTERNAL_SOURCE_LABEL_CHARS = 120
MAX_EXTERNAL_SOURCE_URL_CHARS = 2_048

_SKILL_FRONTMATTER_OPEN_RE = re.compile(r"\A---[ \t]*(?:\r?\n)")
_SKILL_FRONTMATTER_CLOSE_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)
_STANDARD_SKILL_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_NAMESPACED_METADATA_EXTENSIONS = frozenset({"hermes", "openclaw"})


def _compiler_limit_error(
    diagnostics: dict[str, list[dict[str, Any]]] | None,
    *,
    code: str,
    message: str,
    field: str,
    limit: int,
    actual: int,
    source_file: str | None = None,
    **context: Any,
) -> None:
    """Record a stable fail-closed compiler diagnostic for a bounded field."""
    if diagnostics is None:
        return
    _diagnostic(
        diagnostics,
        "errors",
        code,
        message,
        field=field,
        limit=limit,
        actual=actual,
        source_file=source_file,
        **context,
    )


def _bounded_sequence(
    values: list[Any],
    *,
    limit: int,
    diagnostics: dict[str, list[dict[str, Any]]] | None,
    field: str,
    source_file: str | None = None,
) -> list[Any]:
    """Retain a safe prefix, but make any loss invalidate the contract."""
    actual = len(values)
    if actual > limit:
        _compiler_limit_error(
            diagnostics,
            code="compiler_field_item_limit_exceeded",
            message="A declarative Skill field exceeds its bounded item limit.",
            field=field,
            limit=limit,
            actual=actual,
            source_file=source_file,
        )
    return values[:limit]


def _bounded_text(
    value: Any,
    *,
    limit: int,
    diagnostics: dict[str, list[dict[str, Any]]] | None,
    field: str,
    source_file: str | None = None,
) -> str:
    text = str(value or "").strip()
    actual = len(text)
    if actual > limit:
        _compiler_limit_error(
            diagnostics,
            code="compiler_field_text_limit_exceeded",
            message="A declarative Skill text field exceeds its bounded character limit.",
            field=field,
            limit=limit,
            actual=actual,
            source_file=source_file,
        )
    return text[:limit]


def _audit_declarative_graph(
    value: Any,
    diagnostics: dict[str, list[dict[str, Any]]],
    *,
    field: str,
    source_file: str | None,
) -> bool:
    """Audit a loaded YAML object graph without recursive Python calls.

    PyYAML intentionally preserves aliases, including aliases that point back
    to their own mapping/list.  Treat a true back-edge as invalid while
    allowing a non-cyclic shared alias.  Container identities are traversed at
    most once, so an alias DAG is charged by its actual graph size rather than
    by an exponentially expanded tree size.
    """
    stack: list[tuple[str, Any, int, str]] = [("enter", value, 0, field)]
    active: set[int] = set()
    visited_depth: dict[int, int] = {}
    counted_containers: set[int] = set()
    node_count = 0
    scalar_chars = 0

    while stack:
        action, node, depth, path = stack.pop()
        is_container = isinstance(node, (dict, list, tuple, set))
        if action == "exit":
            if is_container:
                active.discard(id(node))
            continue

        identity = id(node) if is_container else None
        if identity is None or identity not in counted_containers:
            node_count += 1
            if identity is not None:
                counted_containers.add(identity)
        if node_count > MAX_COMPILER_STRUCTURE_NODES:
            _compiler_limit_error(
                diagnostics,
                code="compiler_structure_node_limit_exceeded",
                message="A declarative Skill YAML graph exceeds the compiler node limit.",
                field=field,
                limit=MAX_COMPILER_STRUCTURE_NODES,
                actual=node_count,
                source_file=source_file,
                yaml_path=path,
            )
            return False
        if depth > MAX_COMPILER_STRUCTURE_DEPTH:
            _compiler_limit_error(
                diagnostics,
                code="compiler_structure_depth_limit_exceeded",
                message="A declarative Skill YAML graph exceeds the compiler nesting limit.",
                field=field,
                limit=MAX_COMPILER_STRUCTURE_DEPTH,
                actual=depth,
                source_file=source_file,
                yaml_path=path,
            )
            return False

        if not is_container:
            scalar_chars += len(str(node)) if node is not None else 0
            if scalar_chars > MAX_COMPILER_SCALAR_CHARS:
                _compiler_limit_error(
                    diagnostics,
                    code="compiler_scalar_chars_limit_exceeded",
                    message="A declarative Skill YAML graph exceeds the aggregate scalar-text limit.",
                    field=field,
                    limit=MAX_COMPILER_SCALAR_CHARS,
                    actual=scalar_chars,
                    source_file=source_file,
                    yaml_path=path,
                )
                return False
            continue

        assert identity is not None
        if identity in active:
            _diagnostic(
                diagnostics,
                "errors",
                "compiler_structure_cycle",
                "A declarative Skill YAML graph contains a recursive alias cycle.",
                field=field,
                source_file=source_file,
                yaml_path=path,
            )
            return False
        previous_depth = visited_depth.get(identity)
        if previous_depth is not None and depth <= previous_depth:
            continue
        # A shared alias may first be encountered through a shallow top-level
        # anchor declaration and later through a much deeper alias chain.  Walk
        # it again only when that path increases the observed depth; this finds
        # deep alias DAGs without unbounded expansion.
        visited_depth[identity] = depth
        active.add(identity)
        stack.append(("exit", node, depth, path))

        children: list[tuple[Any, str]] = []
        if isinstance(node, dict):
            for key, child in node.items():
                key_text = str(key)
                scalar_chars += len(key_text)
                if scalar_chars > MAX_COMPILER_SCALAR_CHARS:
                    _compiler_limit_error(
                        diagnostics,
                        code="compiler_scalar_chars_limit_exceeded",
                        message="A declarative Skill YAML graph exceeds the aggregate scalar-text limit.",
                        field=field,
                        limit=MAX_COMPILER_SCALAR_CHARS,
                        actual=scalar_chars,
                        source_file=source_file,
                        yaml_path=path,
                    )
                    return False
                children.append((child, f"{path}.{key_text}"))
        else:
            children = [
                (child, f"{path}[{index}]")
                for index, child in enumerate(node)
            ]
        for child, child_path in reversed(children):
            stack.append(("enter", child, depth + 1, child_path))
    return True


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


def _restricted_frontmatter_fallback(yaml_content: str) -> Dict[str, Any]:
    """Parse only flat scalar metadata when PyYAML is unavailable.

    Rejecting nested/list/block YAML is safer than pretending it is a series
    of unrelated ``key: value`` strings.  A host without PyYAML can still
    inspect the common flat ``name``/``description`` header, while complex
    standard YAML fails explicitly and can never be miscompiled.
    """
    frontmatter: Dict[str, Any] = {}
    for line_number, line in enumerate(yaml_content.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace() or line.lstrip().startswith(("-", "?")):
            raise FrontmatterParseError(
                "frontmatter_yaml_loader_unavailable",
                "PyYAML is unavailable and nested frontmatter cannot be parsed safely.",
                context={"line": line_number},
            )
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_.-]*):(?:\s*(.*))?", line)
        if match is None:
            raise FrontmatterParseError(
                "frontmatter_yaml_loader_unavailable",
                "PyYAML is unavailable and this frontmatter is outside the restricted scalar subset.",
                context={"line": line_number},
            )
        key = match.group(1)
        value = (match.group(2) or "").strip()
        if key in frontmatter:
            raise FrontmatterParseError(
                "duplicate_frontmatter_key",
                f"SKILL.md frontmatter repeats key {key!r}.",
                context={"key": key, "line": line_number},
            )
        if not value or value.startswith(("[", "{", "|", ">", "&", "*", "!")):
            raise FrontmatterParseError(
                "frontmatter_yaml_loader_unavailable",
                "PyYAML is unavailable and complex frontmatter cannot be parsed safely.",
                context={"key": key, "line": line_number},
            )
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        frontmatter[key] = value
    return frontmatter


def parse_frontmatter(
    content: str,
    *,
    strict: bool = False,
) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    PyYAML SafeLoader is authoritative and duplicate keys are rejected before
    construction.  If PyYAML is unavailable, only a deliberately flat scalar
    subset is accepted.  ``strict=True`` raises ``FrontmatterParseError`` so
    package compilation can publish a stable fail-closed diagnostic; the
    compatibility mode returns an empty mapping for rejected frontmatter but
    never reinterprets malformed YAML with a second parser.

    Returns:
        (frontmatter_dict, remaining_body)
    """
    frontmatter: Dict[str, Any] = {}
    body = content

    open_match = _SKILL_FRONTMATTER_OPEN_RE.match(content)
    if open_match is None:
        return frontmatter, body

    search_end = min(
        len(content),
        open_match.end() + MAX_SKILL_FRONTMATTER_CHARS + 1,
    )
    end_match = _SKILL_FRONTMATTER_CLOSE_RE.search(
        content,
        open_match.end(),
        search_end,
    )
    if not end_match:
        if len(content) - open_match.end() > MAX_SKILL_FRONTMATTER_CHARS:
            error = FrontmatterParseError(
                "frontmatter_source_limit_exceeded",
                "SKILL.md YAML frontmatter exceeds its bounded character limit.",
                context={
                    "limit": MAX_SKILL_FRONTMATTER_CHARS,
                    "actual_at_least": MAX_SKILL_FRONTMATTER_CHARS + 1,
                },
            )
            if strict:
                raise error
            logger.warning("Rejected oversized Skill frontmatter")
            return frontmatter, body
        if strict:
            raise FrontmatterParseError(
                "unclosed_frontmatter",
                "YAML frontmatter starts with '---' but has no closing delimiter.",
            )
        return frontmatter, body

    yaml_content = content[open_match.end():end_match.start()]
    body_start = end_match.end()
    if body_start < len(content) and content[body_start] == "\n":
        body_start += 1
    body = content[body_start:]

    loader = _get_yaml_loader()
    if loader:
        duplicates = _find_duplicate_yaml_keys(yaml_content)
        if duplicates:
            duplicate = duplicates[0]
            error = FrontmatterParseError(
                "duplicate_frontmatter_key",
                "YAML frontmatter contains a duplicate mapping key.",
                context={
                    "key": duplicate.get("key"),
                    "yaml_path": ".".join(duplicate.get("path") or []),
                },
            )
            if strict:
                raise error
            logger.warning("Rejected duplicate Skill frontmatter key: %s", error.context)
            return {}, body
        try:
            parsed = loader(yaml_content)
        except Exception as exc:
            error = FrontmatterParseError(
                "invalid_frontmatter_yaml",
                f"Could not parse YAML frontmatter: {exc}",
            )
            if strict:
                raise error from exc
            logger.warning("Rejected invalid Skill frontmatter YAML: %s", exc)
            return {}, body
        if parsed is None:
            return {}, body
        if not isinstance(parsed, dict):
            error = FrontmatterParseError(
                "invalid_frontmatter_document",
                "YAML frontmatter must contain a mapping at its root.",
            )
            if strict:
                raise error
            logger.warning("Rejected non-mapping Skill frontmatter")
            return {}, body
        frontmatter = parsed
    else:
        try:
            frontmatter = _restricted_frontmatter_fallback(yaml_content)
        except FrontmatterParseError:
            if strict:
                raise
            logger.warning(
                "Could not parse complex Skill frontmatter without PyYAML"
            )
            return {}, body

    return frontmatter, body


def read_skill_frontmatter_source(path: Path) -> str:
    """Read one complete, bounded SKILL.md frontmatter document.

    Catalog discovery needs only metadata, not an arbitrary prefix of the
    instruction body.  Reading one bounded prefix and requiring the closing
    delimiter within it preserves YAML block scalars while keeping scanner
    memory independent of the size of the Markdown body.  The returned text
    ends at the real delimiter and is parsed by :func:`parse_frontmatter`, the
    same parser used by full package loading.
    """
    with path.open("r", encoding="utf-8") as stream:
        prefix = stream.read(MAX_SKILL_FRONTMATTER_CHARS + 8)
    open_match = _SKILL_FRONTMATTER_OPEN_RE.match(prefix)
    if open_match is None:
        raise FrontmatterParseError(
            "missing_skill_frontmatter",
            "SKILL.md must start with a YAML frontmatter delimiter.",
        )
    end_match = _SKILL_FRONTMATTER_CLOSE_RE.search(
        prefix,
        open_match.end(),
        min(len(prefix), open_match.end() + MAX_SKILL_FRONTMATTER_CHARS + 1),
    )
    if end_match is None:
        if len(prefix) - open_match.end() > MAX_SKILL_FRONTMATTER_CHARS:
            raise FrontmatterParseError(
                "frontmatter_source_limit_exceeded",
                "SKILL.md YAML frontmatter exceeds its bounded character limit.",
                context={"limit": MAX_SKILL_FRONTMATTER_CHARS},
            )
        raise FrontmatterParseError(
            "unclosed_frontmatter",
            "YAML frontmatter starts with '---' but has no closing delimiter.",
        )
    return prefix[:end_match.end()] + "\n"


def validate_skill_manifest(
    frontmatter: dict[str, Any],
    *,
    directory_name: str,
    enforce_directory_match: bool = False,
) -> dict[str, Any]:
    """Validate standard Agent Skills metadata without domain assumptions.

    Standard fields follow agentskills.io exactly.  The harness continues to
    recognize two established, namespaced metadata mappings and sequence-form
    ``allowed-tools`` as explicit compatibility extensions; neither is
    silently presented as standard input.  Unknown top-level fields remain
    available to the generic declarative compiler.
    """
    diagnostics: dict[str, Any] = {"errors": [], "warnings": [], "info": []}

    def issue(level: str, code: str, message: str, **context: Any) -> None:
        item = {"code": code, "message": message, "source_file": "SKILL.md"}
        item.update({
            key: value
            for key, value in context.items()
            if value not in (None, "", [], {})
        })
        diagnostics[level].append(item)

    name = frontmatter.get("name")
    if name is None:
        issue("errors", "missing_skill_name", "Agent Skill frontmatter requires a name field.")
    elif not isinstance(name, str):
        issue("errors", "invalid_skill_name_type", "Agent Skill name must be a string.")
    elif not (1 <= len(name) <= 64) or _STANDARD_SKILL_NAME_RE.fullmatch(name) is None:
        issue(
            "errors",
            "invalid_skill_name",
            "Agent Skill name must be 1-64 lowercase ASCII letters, digits, or single hyphens, without leading, trailing, or consecutive hyphens.",
            declaration=name[:80],
        )
    elif name != directory_name:
        level = "errors" if enforce_directory_match else "warnings"
        issue(
            level,
            "skill_name_directory_mismatch",
            "Agent Skill name must match its immediate parent directory name.",
            declared_name=name,
            directory_name=directory_name,
        )

    description = frontmatter.get("description")
    if description is None:
        issue(
            "errors",
            "missing_skill_description",
            "Agent Skill frontmatter requires a non-empty description field.",
        )
    elif not isinstance(description, str):
        issue(
            "errors",
            "invalid_skill_description_type",
            "Agent Skill description must be a string.",
        )
    elif not description.strip():
        issue(
            "errors",
            "empty_skill_description",
            "Agent Skill description must not be empty.",
        )
    elif len(description) > 1024:
        issue(
            "errors",
            "skill_description_too_long",
            "Agent Skill description exceeds the 1024-character limit.",
            limit=1024,
            actual=len(description),
        )

    compatibility = frontmatter.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str):
            issue(
                "errors",
                "invalid_skill_compatibility_type",
                "Agent Skill compatibility must be a string when provided.",
            )
        elif not compatibility.strip() or len(compatibility) > 500:
            issue(
                "errors",
                "invalid_skill_compatibility",
                "Agent Skill compatibility must contain 1-500 characters when provided.",
                limit=500,
                actual=len(compatibility),
            )

    license_value = frontmatter.get("license")
    if license_value is not None and not isinstance(license_value, str):
        issue(
            "errors",
            "invalid_skill_license_type",
            "Agent Skill license must be a string when provided.",
        )

    metadata = frontmatter.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            issue(
                "errors",
                "invalid_skill_metadata_type",
                "Agent Skill metadata must be a mapping from string keys to string values.",
            )
        else:
            for key, value in metadata.items():
                if not isinstance(key, str):
                    issue(
                        "errors",
                        "invalid_skill_metadata_key",
                        "Agent Skill metadata keys must be strings.",
                    )
                    continue
                if isinstance(value, str):
                    continue
                if key in _NAMESPACED_METADATA_EXTENSIONS and isinstance(value, dict):
                    issue(
                        "warnings",
                        "nonstandard_namespaced_metadata_extension",
                        "A namespaced harness metadata mapping is accepted as a compatibility extension; standard Agent Skill metadata values are strings.",
                        field=f"metadata.{key}",
                    )
                    continue
                issue(
                    "errors",
                    "invalid_skill_metadata_value",
                    "Agent Skill metadata values must be strings.",
                    field=f"metadata.{key}",
                )

    allowed_tools = frontmatter.get("allowed-tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        if isinstance(allowed_tools, list) and len(allowed_tools) <= MAX_ENVIRONMENT_CONTRACT_ITEMS and all(
            isinstance(item, str) for item in allowed_tools
        ):
            issue(
                "warnings",
                "nonstandard_allowed_tools_sequence",
                "Sequence-form allowed-tools is accepted as a bounded harness compatibility extension; the standard field is a space-separated string.",
                actual=len(allowed_tools),
            )
        else:
            issue(
                "errors",
                "invalid_skill_allowed_tools_type",
                "Agent Skill allowed-tools must be a space-separated string; the harness compatibility sequence may contain at most 256 strings.",
            )

    _audit_declarative_graph(
        frontmatter,
        diagnostics,
        field="skill_frontmatter",
        source_file="SKILL.md",
    )
    _refresh_diagnostic_summary(diagnostics)
    return diagnostics


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


def _skill_body_template_enabled(frontmatter: dict[str, Any]) -> bool:
    """Return whether a namespaced, versioned extension templates SKILL.md.

    Agent Skill Markdown is instruction text and may legitimately teach or
    demonstrate shell syntax containing ``${SKILL_DIR}`` or ``${SESSION_ID}``.
    It therefore remains byte-for-byte literal by default.  Body expansion is
    a harness compatibility extension, never an inference from token presence.
    """
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return False
    for namespace in _NAMESPACED_METADATA_EXTENSIONS:
        extension = metadata.get(namespace)
        if not isinstance(extension, dict):
            continue
        declaration = extension.get("body_template")
        if not isinstance(declaration, dict):
            continue
        version = declaration.get("schema_version") or declaration.get("version")
        if str(version or "").strip() != "1":
            continue
        if declaration.get("enabled") is True:
            return True
    return False


def load_skill_content(
    skill_path: Path,
    skill_dir: str | None = None,
    session_id: str | None = None,
) -> Dict[str, Any]:
    """Load and parse a SKILL.md file, returning structured data.

    Args:
        skill_path: Path to the SKILL.md file.
        skill_dir: Absolute path to the skill's directory (for explicitly
            templated runtime configuration and opted-in instruction bodies).
        session_id: Session identifier for explicitly templated values.

    Returns:
        Dict with keys: name, description, content (processed), tags,
        related_skills, linked_files, frontmatter (raw).
    """
    declared_skill_root = Path(skill_dir) if skill_dir else skill_path.parent
    root_check = validate_skill_root(declared_skill_root)
    if not root_check.valid or root_check.path is None:
        code = root_check.code or "invalid_skill_root"
        message = root_check.message or "Skill package root failed validation."
        return {
            "error": f"Cannot load Skill package: {message}",
            "package_diagnostics": {
                "valid": False,
                "errors": [{"code": code, "message": message}],
            },
        }
    skill_dir_path = root_check.path
    main_check = validate_skill_resource(
        skill_dir_path,
        skill_path.absolute(),
        expected_kind="file",
    )
    expected_main = skill_dir_path / "SKILL.md"
    if (
        not main_check.valid
        or main_check.path is None
        or main_check.path != expected_main
    ):
        code = main_check.code or "invalid_skill_entrypoint"
        message = main_check.message or "Skill entrypoint must be the root SKILL.md regular file."
        return {
            "error": f"Cannot load Skill package: {message}",
            "package_diagnostics": {
                "valid": False,
                "errors": [{"code": code, "message": message}],
            },
        }

    try:
        raw = main_check.path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        return {"error": f"Cannot read skill file: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error reading skill: {e}"}

    try:
        if _SKILL_FRONTMATTER_OPEN_RE.match(raw) is None:
            raise FrontmatterParseError(
                "missing_skill_frontmatter",
                "SKILL.md must start with YAML frontmatter containing name and description.",
            )
        frontmatter, body = parse_frontmatter(raw, strict=True)
    except FrontmatterParseError as exc:
        diagnostics: dict[str, Any] = {
            "errors": [
                {
                    "code": exc.code,
                    "message": str(exc),
                    **exc.context,
                    "source_file": "SKILL.md",
                }
            ],
            "warnings": [],
            "info": [],
        }
        _refresh_diagnostic_summary(diagnostics)
        return {
            "error": "Cannot compile Skill package: invalid SKILL.md frontmatter.",
            "package_diagnostics": diagnostics,
        }
    frontmatter_diagnostics = validate_skill_manifest(
        frontmatter,
        directory_name=skill_dir_path.name,
        # Canonical package discovery enforces this as an error.  Direct
        # compiler callers retain a diagnostic compatibility path because
        # historical integrations compile a staged directory before it is
        # atomically renamed to the canonical Skill name.
        enforce_directory_match=False,
    )
    if frontmatter_diagnostics.get("errors"):
        return {
            "error": "Cannot compile Skill package: invalid Agent Skill manifest.",
            "package_diagnostics": frontmatter_diagnostics,
        }
    # Standard Skill Markdown is literal.  Only an explicit versioned,
    # namespaced compatibility declaration turns the instruction body into a
    # template; runtime configuration fields are resolved independently below.
    content = body
    if _skill_body_template_enabled(frontmatter):
        content = substitute_template_vars(
            body,
            skill_dir=str(skill_dir_path),
            session_id=session_id,
        )

    name = str(frontmatter.get("name") or skill_dir_path.name)[:64]
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
    workflow_contract = _discover_workflow_contract(
        skill_dir_path,
        linked_files,
        content,
        frontmatter=frontmatter,
    )
    runtime_profile_manifest: dict[str, Any] | None = None
    script_candidates = (
        workflow_contract.get("script_candidates")
        if isinstance(workflow_contract, dict) else None
    )
    if isinstance(script_candidates, list) and script_candidates:
        # This runtime-owned manifest is derived from one immutable package
        # snapshot. Route activation later selects only exact entrypoints from
        # the chosen plan, so an unrelated browser helper cannot change a
        # different route's executor profile.
        try:
            from tools.skill_runtime_profile import (
                compile_skill_runtime_profile_manifest,
            )

            runtime_profile_manifest = (
                compile_skill_runtime_profile_manifest(
                    skill_dir_path,
                    tuple(
                        str(path)
                        for path in script_candidates
                        if isinstance(path, str) and path
                    ),
                )
            )
        except (ImportError, RuntimeError, ValueError) as exc:
            runtime_profile_manifest = {
                "schema_version": 1,
                "valid": False,
                "error_code": str(
                    getattr(exc, "code", None)
                    or "skill_runtime_profile_unavailable"
                ),
                "scripts": [],
            }
        workflow_contract = dict(workflow_contract)
        workflow_contract["_chatds_runtime_profile_manifest"] = (
            runtime_profile_manifest
        )
    execution_contract = workflow_contract.get("execution_contract") or {}
    package_diagnostics = workflow_contract.get("package_diagnostics") or {}
    manifest_notices = {
        level: list(frontmatter_diagnostics.get(level) or [])
        for level in ("warnings", "info")
    }
    if any(manifest_notices.values()):
        if not isinstance(package_diagnostics, dict) or not package_diagnostics:
            package_diagnostics = {"errors": [], "warnings": [], "info": []}
        for level, items in manifest_notices.items():
            package_diagnostics.setdefault(level, []).extend(items)
        _refresh_diagnostic_summary(package_diagnostics)
        workflow_contract = dict(workflow_contract)
        workflow_contract["package_diagnostics"] = package_diagnostics
        if execution_contract:
            execution_contract = dict(execution_contract)
            execution_contract["diagnostics"] = package_diagnostics
            workflow_contract["execution_contract"] = execution_contract

    result: Dict[str, Any] = {
        "name": name,
        "description": description,
        "content": content,
        # Preserve the canonical validated package root for runtime-owned
        # capability compilation. Persistent process grants must hash the
        # entire package with the same algorithm used by lease creation.
        "skill_dir": str(skill_dir_path),
        # Capability-plan identity covers the complete canonical document,
        # including frontmatter fields such as allowed-tools/compatibility.
        # Hashing only the Markdown body would accept a stale plan after an
        # authority-bearing metadata edit.
        "skill_md_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "skill_md_chars": len(raw),
        "tags": tags,
        "related_skills": related_skills,
        "linked_files": linked_files if linked_files else None,
        "resource_graph": resource_graph if resource_graph else None,
        "workflow_contract": workflow_contract if workflow_contract else None,
        "runtime_profile_manifest": runtime_profile_manifest,
        "execution_contract": execution_contract if execution_contract else None,
        "package_diagnostics": package_diagnostics if package_diagnostics else None,
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

# Legacy prose hints are not a source-code/data parser. Executable files and
# structured datasets stay fully discoverable through the resource graph and
# are handled by their dedicated runners/parsers, but must not become legacy
# prose contracts merely because code calls ``save()`` or a bundled data file
# is large. Authoritative YAML is compiled separately below.
_WORKFLOW_SEMANTIC_RESOURCE_SUFFIXES = {
    ".md", ".txt", ".rst",
}


def _discover_linked_files(skill_dir: Path) -> dict[str, list[str]]:
    """Discover linked reference, workflow, template, asset, and script files."""
    linked: dict[str, list[str]] = {}

    for directory in _WORKFLOW_DIRS:
        subdir = skill_dir / directory
        if not validate_skill_resource(
            skill_dir, subdir, expected_kind="directory"
        ).valid:
            continue
        files = _list_resource_files(skill_dir, subdir)
        if files:
            linked[directory] = files

    # Common domain-specific resource directories should be surfaced without
    # knowing their semantics in advance.
    safe_subdirs: list[Path] = []
    try:
        candidates = list(skill_dir.iterdir())
    except OSError:
        candidates = []
    for candidate in candidates:
        check = validate_skill_resource(
            skill_dir,
            candidate,
            expected_kind="directory",
        )
        if check.valid and check.path is not None:
            safe_subdirs.append(check.path)
    for subdir in sorted(safe_subdirs):
        if subdir.name.startswith(".") or subdir.name in linked:
            continue
        if subdir.name in {"__pycache__", "node_modules", ".git"}:
            continue
        files = _list_resource_files(skill_dir, subdir, limit=50)
        if files:
            linked[subdir.name] = files

    # openclaw-compatible .mcp.json (MCP server configuration)
    mcp_json = skill_dir / ".mcp.json"
    if validate_skill_resource(skill_dir, mcp_json, expected_kind="file").valid:
        linked["mcp_config"] = [".mcp.json"]

    root_files = []
    try:
        root_candidates = sorted(skill_dir.iterdir())
    except OSError:
        root_candidates = []
    for child in root_candidates:
        check = validate_skill_resource(skill_dir, child, expected_kind="file")
        if not check.valid or check.path is None or child.name == "SKILL.md":
            continue
        child = check.path
        if child.name.startswith(".") and child.name != ".mcp.json":
            continue
        root_files.append(str(child.relative_to(skill_dir)))
    if root_files:
        linked["root_files"] = root_files

    return linked


def _list_resource_files(skill_dir: Path, directory: Path, limit: int = 200) -> list[str]:
    """List the complete safe resource closure.

    ``limit`` remains for call compatibility, but is intentionally not applied:
    clipping here made later compiler diagnostics impossible and could omit an
    authoritative worker, format, or reference declaration.  UI presentation
    is bounded separately by explicitly labelled samples.
    """
    files: list[str] = []
    for path in iter_safe_regular_files(
        skill_dir,
        directory,
        excluded_dirs={"__pycache__", "node_modules", ".git"},
    ):
        if any(part in {"__pycache__", "node_modules", ".git"} for part in path.parts):
            continue
        files.append(str(path.relative_to(skill_dir)))
    return files


def _discover_resource_graph(
    skill_dir: Path,
    linked_files: dict[str, list[str]],
) -> dict[str, Any]:
    """Return a compact, generic resource graph for progressive disclosure."""
    if not linked_files:
        return {}

    standard_categories = (
        "orchestration", "workers", "workflows", "protocols", "formats",
        "references", "scripts", "evaluation", "examples", "templates",
        "assets",
    )
    # Prefer conventional Agent-Skill resource directories, then surface every
    # other declared package directory deterministically.  Domain directory
    # names must not receive special treatment in the generic harness.
    important_categories = [
        name for name in standard_categories if name in linked_files
    ]
    important_categories.extend(
        name
        for name in sorted(linked_files, key=str.casefold)
        if name not in important_categories
        and name not in {"root_files", "mcp_config"}
    )
    categories = {
        name: {
            "count": len(files),
            "sample": files[:12],
            "sample_truncated": len(files) > 12,
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
        "suggested_files_truncated": len(suggested_files) > 40,
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


_MARKDOWN_FILENAME_RE = re.compile(
    r"`?((?:[^\s`|<>()\[\],;，；:：。！？]+/)*"
    r"[^\s`|<>()\[\],;，；:：。！？]+\.md)`?",
    re.IGNORECASE,
)


def _clean_legacy_markdown_filename(value: str) -> str:
    """Remove explicit prose labels without weakening Unicode path support."""

    path = str(value or "").strip().strip("`")
    labelled = re.match(
        r"^(?:最终报告|最终文件|输出文件|输出报告|报告文件|"
        r"合并文件|合并报告|文件)\s*为\s*(.+\.md)$",
        path,
        re.IGNORECASE,
    )
    if labelled is not None:
        candidate = labelled.group(1).strip().strip("`")
        if _safe_structured_artifact_path(candidate):
            return candidate
    return path


def _extract_declared_modular_markdown_files(text: str) -> list[str]:
    """Extract legacy Markdown modules without a two-digit/ASCII bias."""
    modules: list[str] = []
    # Numbered legacy declarations may occur in prose lists.  Preserve the
    # complete safe relative path and accept any numbering width/Unicode name.
    for match in _MARKDOWN_FILENAME_RE.finditer(text):
        path = _clean_legacy_markdown_filename(match.group(1))
        if (
            _safe_structured_artifact_path(path)
            and not any(token in path for token in ("*", "?"))
            and re.match(r"^\d+(?:[_-]|(?=\.md$))", PurePosixPath(path).name)
        ):
            modules.append(path)
    # Unnumbered modules need explicit table placement, avoiding incidental
    # Markdown references elsewhere in a legacy format guide.
    for row in re.finditer(
        r"^\|\s*`?([^|`\n]+\.md)`?\s*\|\s*([^|\n]+)\|",
        text,
        re.MULTILINE | re.IGNORECASE,
    ):
        path = row.group(1).strip()
        if (
            _safe_structured_artifact_path(path)
            and not any(token in path for token in ("*", "?"))
        ):
            modules.append(path)
    return _dedupe(modules)


def _looks_like_merged_markdown_artifact(filename: str, context: str = "") -> bool:
    """Distinguish a canonical merge target from ancillary Markdown files."""
    lowered = Path(filename).name.lower()
    if re.search(
        r"(?:^|[_-])(?:full|final|merged|combined)"
        r"(?:[_-](?:report|output|artifact|document))?\.md$",
        lowered,
    ):
        return True
    if re.search(
        r"(?:最终|完整|合并)(?:报告|输出|产物|文档)?\.md$",
        lowered,
    ):
        return True
    return bool(
        re.search(
            r"(?:auto[- ]?merge|auto[- ]?merged|merge target|merged output|"
            r"canonical (?:file|report|artifact)|final (?:file|report|artifact)|"
            r"自动合并|合并(?:目标|输出|报告)|最终(?:文件|报告|产物)|"
            r"完整(?:合并)?报告)",
            context,
            re.IGNORECASE,
        )
    )


def _extract_declared_ancillary_files(text: str) -> list[str]:
    """Extract explicitly declared non-modular Markdown deliverables.

    Only packaging/count declarations and sentences that explicitly describe
    an index, audit, manifest, or alongside-generated file are considered.
    General Markdown references elsewhere in a format document are ignored.
    """
    packaging_files: list[str] = []
    supplementary_files: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or ".md" not in stripped.lower():
            continue
        packaging_declaration = bool(
            re.search(
                r"(?:file\s*count|files?\s*\+|=\s*\d+\s*total|"
                r"(?:output\s+files?|deliverables?|artifacts?)\s*"
                r"(?:include|contain|consist(?:s)?\s+of|:)|"
                r"文件(?:总数|数量)|共\s*\d+\s*个?\s*文件)",
                stripped,
                re.IGNORECASE,
            )
        )
        ancillary_declaration = bool(
            re.search(
                r"(?:generated|created|written|produced)\s+alongside|"
                r"\balongside\b|\bindex\b|\baudit\b|\bmanifest\b|\bchecklist\b|"
                r"目录|索引|审计|清单|随同|同时生成",
                stripped,
                re.IGNORECASE,
            )
        )
        if not (packaging_declaration or ancillary_declaration):
            continue
        for match in _MARKDOWN_FILENAME_RE.finditer(stripped):
            filename = _clean_legacy_markdown_filename(match.group(1))
            basename = Path(filename).name
            if re.match(r"^\d+(?:[_-]|(?=\.md$))", basename):
                continue
            if basename.lower() == "skill.md":
                continue
            left_delimiters = [
                stripped.rfind(delimiter, 0, match.start())
                for delimiter in ("|", "+", ",", "，", ";", "；")
            ]
            context_start = max([position for position in left_delimiters if position >= 0] or [-1]) + 1
            right_delimiters = [
                stripped.find(delimiter, match.end())
                for delimiter in ("|", "+", ",", "，", ";", "；")
            ]
            valid_right = [position for position in right_delimiters if position >= 0]
            context_end = min(valid_right) if valid_right else len(stripped)
            if _looks_like_merged_markdown_artifact(
                filename,
                stripped[context_start:context_end],
            ):
                continue
            target = packaging_files if packaging_declaration else supplementary_files
            target.append(filename)
    return _dedupe(packaging_files + supplementary_files)


def _extract_declared_final_markdown_files(text: str) -> list[str]:
    final_files: list[str] = []
    for line in text.splitlines():
        if ".md" not in line.lower():
            continue
        for match in _MARKDOWN_FILENAME_RE.finditer(line):
            filename = _clean_legacy_markdown_filename(match.group(1))
            left_delimiters = [
                line.rfind(delimiter, 0, match.start())
                for delimiter in ("|", "+", ",", "，", ";", "；")
            ]
            context_start = max(
                [position for position in left_delimiters if position >= 0] or [-1]
            ) + 1
            right_delimiters = [
                line.find(delimiter, match.end())
                for delimiter in ("|", "+", ",", "，", ";", "；")
            ]
            valid_right = [position for position in right_delimiters if position >= 0]
            context_end = min(valid_right) if valid_right else len(line)
            if _looks_like_merged_markdown_artifact(
                filename,
                line[context_start:context_end],
            ):
                final_files.append(filename)
    return _dedupe(final_files)


def _extract_declared_artifact_indexes(text: str) -> list[str]:
    """Extract artifacts explicitly labelled as an index; never infer by filename."""
    index_files: list[str] = []
    for line in text.splitlines():
        if ".md" not in line.lower():
            continue
        for match in _MARKDOWN_FILENAME_RE.finditer(line):
            filename = _clean_legacy_markdown_filename(match.group(1))
            left_delimiters = [
                line.rfind(delimiter, 0, match.start())
                for delimiter in ("|", "+", ",", "，", ";", "；")
            ]
            context_start = max(
                [position for position in left_delimiters if position >= 0] or [-1]
            ) + 1
            right_delimiters = [
                line.find(delimiter, match.end())
                for delimiter in ("|", "+", ",", "，", ";", "；")
            ]
            valid_right = [position for position in right_delimiters if position >= 0]
            context_end = min(valid_right) if valid_right else len(line)
            context = line[context_start:context_end]
            if re.search(
                r"\bindex\b|\btable\s+of\s+contents\b|目录|索引",
                context,
                re.IGNORECASE,
            ):
                index_files.append(filename)
    return _dedupe(index_files)


def _declares_exact_artifact_set(text: str) -> bool:
    for line in text.splitlines():
        if not re.search(
            r"\bexact(?:ly)?\b.{0,30}\b\d+\s+(?:files?|artifacts?|deliverables?)\b|"
            r"\b\d+\s+(?:files?|artifacts?|deliverables?)\b.{0,30}\bexact(?:ly)?\b|"
            r"\bonly\s+(?:the\s+)?(?:following|listed|declared)\b|"
            r"\bno\s+additional\s+(?:files?|artifacts?|deliverables?)\b|"
            r"(?:package|manifest|artifact\s+set).{0,40}\bexact(?:ly)?\b|"
            r"\bexact(?:ly)?\b.{0,40}(?:package|manifest|artifact\s+set)|"
            r"(?:精确|恰好)\s*\d+\s*个?(?:文件|交付物|产物)|"
            r"仅(?:包含|限于|以下)|不得(?:包含|生成)额外",
            line,
            re.IGNORECASE,
        ):
            continue
        if re.search(r"\bnot\s+exact|并非精确|不要求精确", line, re.IGNORECASE):
            continue
        if re.search(
            r"files?|artifacts?|deliverables?|package|manifest|文件|交付物|产物",
            line,
            re.IGNORECASE,
        ):
            return True
    return False


def _parse_total_file_count(text: str) -> int | None:
    for line in text.splitlines():
        equation = re.search(r"=\s*(\d+)\s*total", line, re.IGNORECASE)
        if equation and re.search(
            r"(?:files?|artifacts?|deliverables?|\.md\b|文件|交付物)",
            line,
            re.IGNORECASE,
        ):
            return int(equation.group(1))
    patterns = (
        r"\btotal(?:\s+(?:file|artifact|deliverable))?\s*count\b[^0-9]{0,20}(\d+)",
        r"\b(\d+)\s+(?:files?|artifacts?|deliverables?)\s+(?:in\s+)?total\b",
        r"(?:文件|交付物)(?:总数|数量)[^0-9]{0,20}(\d+)",
        r"共\s*(\d+)\s*个?\s*(?:文件|交付物)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _parse_declared_modular_file_count(text: str) -> int | None:
    match = re.search(
        r"\b(\d+)\s+(?:(?:numbered|content|module|modular|report)\s+)+files?\b|"
        r"(\d+)\s*个?(?:编号|内容|模块化?|报告)文件",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _declares_markdown_output_packaging(
    text: str,
    markdown_files: list[str],
    *,
    total_count: int | None,
) -> bool:
    """Return true only for format docs that actually define an output package."""
    if not markdown_files:
        return False
    if total_count is not None:
        return True
    if re.search(
        r"^#{1,6}\s+(?:(?:output|report|deliverable|artifact|module|modular)\s+)?"
        r"(?:files?|package|packaging|manifest|文件|交付物)\s*$|"
        r"^#{1,6}\s+.*(?:"
        r"(?:section|part|module)\s*(?:→|->|to)\s*file\s+mapping|"
        r"file\s+(?:count|naming|manifest)|output\s+rules|"
        r"章节.{0,10}文件映射|文件命名|输出规则).*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        return True
    if len(markdown_files) >= 2 and re.search(
        r"^#{1,6}\s+.*(?:output|deliverable|artifact|report|输出|交付物|报告)"
        r".*(?:format|layout|structure|格式|布局|结构)\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        return True
    if re.search(
        r"^(?:[-*]\s+)?(?:\*\*)?"
        r"(?:output\s+files?|deliverables?|artifacts?|输出文件|交付物)"
        r"(?:\*\*)?\s*:",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        return True
    return bool(
        re.search(
            r"^\|\s*(?:file|filename|artifact|deliverable|文件|文件名|交付物)\s*\|",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        and len(markdown_files) >= 2
    )


def _parse_orchestrator_contract(
    skill_dir: Path,
    *,
    authorized_resources: set[str] | None = None,
) -> dict[str, Any]:
    """Parse structured YAML orchestrator contracts under orchestration/ and workflows/.

    Reads the skill's OWN declared final_report_template (sections + auto_merge) so the
    harness executes the skill's intent instead of inferring it from prose. Returns an
    empty dict when no orchestrator YAML declares a final report template.
    """
    if not _get_yaml_loader():
        return {}
    candidates: list[Path] = []
    for sub in ("orchestration", "workflows", "orchestrator"):
        base = skill_dir / sub
        candidates.extend(
            path
            for path in iter_safe_regular_files(skill_dir, base)
            if path.parent == base and path.suffix.lower() in {".yaml", ".yml"}
        )
    candidates.extend(
        path
        for path in iter_safe_regular_files(skill_dir, skill_dir)
        if path.parent == skill_dir
        and path.name.lower().startswith("orchestrat")
        and path.suffix.lower() in {".yaml", ".yml"}
    )

    contract: dict[str, Any] = {}
    for path in _dedupe_paths_local(candidates):
        relative_path = str(path.relative_to(skill_dir))
        if (
            authorized_resources is not None
            and relative_path not in authorized_resources
        ):
            continue
        # This lightweight output pre-pass must use the same graph-safe loader
        # as the authoritative compiler.  Its local diagnostics are discarded
        # because the compiler loads the same file immediately afterwards and
        # publishes them in package_diagnostics.
        local_diagnostics: dict[str, list[dict[str, Any]]] = {
            "errors": [],
            "warnings": [],
            "info": [],
        }
        data = _load_yaml_mapping(
            path,
            skill_dir,
            local_diagnostics,
            kind="orchestrator",
        )
        if data is None:
            continue
        structured_output = _normalize_structured_package_output_contract(
            data.get("output_contract"),
            skill_dir=skill_dir,
            source_file=str(path.relative_to(skill_dir)),
            diagnostics=local_diagnostics,
        )
        if structured_output:
            contract.update(structured_output)
        template = data.get("final_report_template")
        if not isinstance(template, dict):
            continue

        sections = template.get("sections")
        if isinstance(sections, list):
            declared_sections = [s for s in sections if isinstance(s, dict) and s.get("section")]
            if declared_sections:
                contract.setdefault("declared_section_count", len(declared_sections))
                contract.setdefault(
                    "section_titles",
                    [str(s.get("section")) for s in declared_sections],
                )

        auto_merge = template.get("auto_merge")
        if isinstance(auto_merge, dict):
            output_artifact = auto_merge.get("output_artifact")
            command = auto_merge.get("command_template") or auto_merge.get("command")
            if output_artifact or command:
                # Preserve the legacy declaration's three states.  Omitted
                # ``mandatory`` lets an explicit merge command opt in through
                # the compatibility rule; explicit false disables byte merge.
                mandatory = auto_merge.get("mandatory")
                if isinstance(mandatory, bool):
                    contract.setdefault("merge_mandatory", mandatory)
                if output_artifact:
                    contract.setdefault(
                        "declared_final_artifact",
                        str(output_artifact).strip(),
                    )
                if command:
                    contract.setdefault("merge_command", str(command).strip())
                size_range = auto_merge.get("expected_size_range")
                if isinstance(size_range, str) and "-" in size_range:
                    low, _, high = size_range.partition("-")
                    low_bytes = _parse_bytes_literal(low)
                    high_bytes = _parse_bytes_literal(high)
                    if low_bytes:
                        contract.setdefault("expected_min_bytes", low_bytes)
                    if high_bytes:
                        contract.setdefault("expected_max_bytes", high_bytes)
                checks = auto_merge.get("post_merge_verification")
                if isinstance(checks, list):
                    parsed_checks = [str(c).strip() for c in checks if str(c).strip()]
                    if parsed_checks:
                        contract["post_merge_checks"] = parsed_checks
                        for check in parsed_checks:
                            min_lines = re.search(r"line count\s*>\s*([\d,]+)|>\s*([\d,]+)\s*lines", check, re.IGNORECASE)
                            if min_lines:
                                value = _parse_int_literal(min_lines.group(1) or min_lines.group(2))
                                if value:
                                    contract.setdefault("expected_min_lines", value)
                            min_bytes = re.search(r"size\s*>\s*([\d.]+\s*[kmg]?b)|>\s*([\d.]+\s*[kmg]b)", check, re.IGNORECASE)
                            if min_bytes and "expected_min_bytes" not in contract:
                                value = _parse_bytes_literal(min_bytes.group(1) or min_bytes.group(2))
                                if value:
                                    contract.setdefault("expected_min_bytes", value)
    return contract


def _parse_output_format_contract(
    skill_dir: Path,
    *,
    authorized_resources: set[str] | None = None,
) -> dict[str, Any]:
    """Parse formats/*.md output-packaging specs (declared file count, modular files).

    The output format spec is the Skill's legacy declaration of how a Markdown
    package is assembled. Structured YAML ``output_contract`` declarations are
    preferred; this parser remains a compatibility fallback.
    """
    formats_dir = skill_dir / "formats"
    if not validate_skill_resource(
        skill_dir, formats_dir, expected_kind="directory"
    ).valid:
        return {}
    contract: dict[str, Any] = {}
    modular_files: list[str] = []
    ancillary_files: list[str] = []
    final_files: list[str] = []
    artifact_indexes: list[str] = []
    exact_artifact_policies: list[dict[str, Any]] = []
    format_declarations: list[dict[str, Any]] = []
    output_format_files: list[str] = []
    for path in (
        candidate
        for candidate in iter_safe_regular_files(skill_dir, formats_dir)
        if candidate.parent == formats_dir and candidate.suffix.lower() == ".md"
    ):
        relative_path = str(path.relative_to(skill_dir))
        if (
            authorized_resources is not None
            and relative_path not in authorized_resources
        ):
            continue
        text, _, source_truncated, source_unreadable = (
            _read_semantic_text_resource(path, skill_dir)
        )
        # This is a compiler path, not a presentation preview.  Parsing a
        # prefix would silently lose authoritative declarations that occur
        # after the preview limit.  The package-wide semantic scan records a
        # fail-closed diagnostic for truncated/unreadable authoritative
        # resources; this parser must simply decline to infer from them.
        if source_truncated or source_unreadable or not text:
            continue
        path_ancillary_files = _extract_declared_ancillary_files(text)
        path_final_files = _extract_declared_final_markdown_files(text)
        path_modular_files = [
            candidate
            for candidate in _extract_declared_modular_markdown_files(text)
            if candidate not in path_ancillary_files
            and candidate not in path_final_files
            and PurePosixPath(candidate).name.casefold()
            not in {"skill.md", "readme.md", "_checklist.md", "checklist.md"}
        ]
        path_artifact_indexes = _extract_declared_artifact_indexes(text)
        markdown_files = _dedupe(
            path_modular_files + path_ancillary_files + path_final_files
        )
        total_count = _parse_total_file_count(text)
        if not _declares_markdown_output_packaging(
            text,
            markdown_files,
            total_count=total_count,
        ):
            continue

        modular_count = _parse_declared_modular_file_count(text)
        if total_count is not None and "declared_file_count" not in contract:
            contract["declared_file_count"] = total_count
        if modular_count is not None and "declared_modular_file_count" not in contract:
            contract["declared_modular_file_count"] = modular_count
        modular_files.extend(path_modular_files)
        ancillary_files.extend(path_ancillary_files)
        final_files.extend(path_final_files)
        artifact_indexes.extend(path_artifact_indexes)
        output_format_files.append(relative_path)
        declaration = {
            "source_file": relative_path,
            "declared_file_count": total_count,
            "declared_modular_file_count": modular_count,
            "modular_files": path_modular_files,
            "ancillary_files": path_ancillary_files,
            "final_artifacts": path_final_files,
            "artifact_indexes": path_artifact_indexes,
        }
        if _declares_exact_artifact_set(text):
            policy_artifacts = _dedupe(
                path_modular_files
                + path_ancillary_files
                + path_final_files
            )
            policy = {
                "mode": "exact",
                "source_file": relative_path,
                "declared_file_count": total_count,
            }
            if total_count is None or len(policy_artifacts) == total_count:
                policy["artifacts"] = policy_artifacts
            declaration["artifact_set_policy"] = policy
            exact_artifact_policies.append(policy)
        format_declarations.append(declaration)
    if modular_files:
        contract["declared_modular_files"] = _dedupe(modular_files)
    if ancillary_files:
        contract["declared_ancillary_files"] = _dedupe(ancillary_files)
    if final_files:
        unique_final_files = _dedupe(final_files)
        contract["declared_format_final_artifacts"] = unique_final_files
        if len(unique_final_files) == 1:
            contract["declared_final_artifact"] = unique_final_files[0]
    unique_artifact_indexes = _dedupe(artifact_indexes)
    if len(unique_artifact_indexes) == 1:
        contract["artifact_index"] = unique_artifact_indexes[0]
    elif unique_artifact_indexes:
        contract["artifact_indexes"] = unique_artifact_indexes
    if len(exact_artifact_policies) == 1:
        contract["artifact_set_policy"] = exact_artifact_policies[0]
    elif exact_artifact_policies:
        contract["artifact_set_policies"] = exact_artifact_policies
    if format_declarations:
        contract["format_declarations"] = format_declarations
    if output_format_files:
        contract["output_format_files"] = _dedupe(output_format_files)
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


def _orchestrator_yaml_candidates(skill_dir: Path) -> list[Path]:
    """Return YAML files that may define orchestration, excluding worker configs."""
    candidates: list[Path] = []
    for sub in ("orchestration", "workflows", "orchestrator"):
        base = skill_dir / sub
        for path in iter_safe_regular_files(
            skill_dir,
            base,
            excluded_dirs={"__pycache__", "node_modules", ".git"},
        ):
            if path.suffix.lower() not in {".yaml", ".yml"}:
                continue
            relative_parts = path.relative_to(base).parts[:-1]
            if any(part.lower() in {"worker", "workers", "agents"} for part in relative_parts):
                continue
            candidates.append(path)
    candidates.extend(
        path
        for path in iter_safe_regular_files(skill_dir, skill_dir)
        if path.parent == skill_dir
        and path.name.lower().startswith("orchestrat")
        and path.suffix.lower() in {".yaml", ".yml"}
    )
    return _dedupe_paths_local(candidates)


def _diagnostic(
    diagnostics: dict[str, list[dict[str, Any]]],
    level: str,
    code: str,
    message: str,
    **context: Any,
) -> None:
    item = {"code": code, "message": message}
    item.update({key: value for key, value in context.items() if value not in (None, "", [], {})})
    diagnostics.setdefault(level, []).append(item)


def _refresh_diagnostic_summary(
    diagnostics: dict[str, Any],
) -> None:
    errors = diagnostics.get("errors") or []
    warnings = diagnostics.get("warnings") or []
    info = diagnostics.get("info") or []
    diagnostics["valid"] = not errors
    diagnostics["summary"] = {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "info_count": len(info),
    }


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        result: list[str] = []
        for key, enabled in value.items():
            if enabled is False or enabled is None:
                continue
            result.append(str(key).strip())
        return _dedupe(result)
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            if isinstance(item, dict):
                worker_id = item.get("worker_id") or item.get("worker") or item.get("id") or item.get("name")
                if worker_id:
                    result.append(str(worker_id).strip())
            elif item is not None:
                result.append(str(item).strip())
        return _dedupe(result)
    return [str(value).strip()]


def _compact_mapping(
    value: Any,
    *,
    max_items: int = 40,
    max_text: int = 2_000,
    diagnostics: dict[str, list[dict[str, Any]]] | None = None,
    field: str = "declarative_value",
    source_file: str | None = None,
) -> Any:
    """Keep declarative values JSON-friendly, bounded, and cycle-safe.

    The old implementation silently sliced mappings/lists/strings and followed
    YAML aliases recursively.  This implementation still retains a bounded
    prefix for safe diagnostics and observability, but any omitted declaration
    is an explicit compiler error, so the resulting execution contract cannot
    be reported as valid.
    """
    active: set[int] = set()
    memo: dict[int, Any] = {}
    node_count = 0
    scalar_chars = 0
    reported_node_limit = False
    reported_scalar_limit = False
    reported_cycles: set[int] = set()

    def compact(node: Any, *, depth: int, path: str) -> Any:
        nonlocal node_count, scalar_chars, reported_node_limit, reported_scalar_limit
        node_count += 1
        if node_count > MAX_COMPACT_STRUCTURE_NODES:
            if not reported_node_limit:
                reported_node_limit = True
                _compiler_limit_error(
                    diagnostics,
                    code="compiler_structure_node_limit_exceeded",
                    message="A declarative Skill field exceeds the compiler node limit.",
                    field=field,
                    limit=MAX_COMPACT_STRUCTURE_NODES,
                    actual=node_count,
                    source_file=source_file,
                    yaml_path=path,
                )
            return None
        if depth > MAX_COMPACT_STRUCTURE_DEPTH:
            _compiler_limit_error(
                diagnostics,
                code="compiler_structure_depth_limit_exceeded",
                message="A declarative Skill field exceeds the compiler nesting limit.",
                field=field,
                limit=MAX_COMPACT_STRUCTURE_DEPTH,
                actual=depth,
                source_file=source_file,
                yaml_path=path,
            )
            return None

        is_container = isinstance(node, (dict, list, tuple, set))
        if is_container:
            identity = id(node)
            if identity in active:
                if identity not in reported_cycles:
                    reported_cycles.add(identity)
                    if diagnostics is not None:
                        _diagnostic(
                            diagnostics,
                            "errors",
                            "compiler_structure_cycle",
                            "A declarative Skill field contains a recursive alias cycle.",
                            field=field,
                            source_file=source_file,
                            yaml_path=path,
                        )
                return None
            if identity in memo:
                return memo[identity]

            actual = len(node)
            if actual > max_items:
                _compiler_limit_error(
                    diagnostics,
                    code="compiler_field_item_limit_exceeded",
                    message="A declarative Skill field exceeds its bounded item limit.",
                    field=path,
                    limit=max_items,
                    actual=actual,
                    source_file=source_file,
                )
            active.add(identity)
            if isinstance(node, dict):
                result: Any = {}
                memo[identity] = result
                for index, (key, item) in enumerate(node.items()):
                    if index >= max_items:
                        break
                    key_text = str(key)
                    scalar_chars += len(key_text)
                    result[key_text] = compact(
                        item,
                        depth=depth + 1,
                        path=f"{path}.{key_text}",
                    )
            else:
                result = []
                memo[identity] = result
                for index, item in enumerate(node):
                    if index >= max_items:
                        break
                    result.append(
                        compact(
                            item,
                            depth=depth + 1,
                            path=f"{path}[{index}]",
                        )
                    )
            active.remove(identity)
            return result

        if isinstance(node, str):
            text = node
        elif isinstance(node, (int, float, bool)) or node is None:
            text = "" if node is None else str(node)
        else:
            text = str(node)
        scalar_chars += len(text)
        if scalar_chars > MAX_COMPACT_SCALAR_CHARS and not reported_scalar_limit:
            reported_scalar_limit = True
            _compiler_limit_error(
                diagnostics,
                code="compiler_scalar_chars_limit_exceeded",
                message="A declarative Skill field exceeds the aggregate scalar-text limit.",
                field=field,
                limit=MAX_COMPACT_SCALAR_CHARS,
                actual=scalar_chars,
                source_file=source_file,
                yaml_path=path,
            )
        if isinstance(node, str):
            if len(node) > max_text:
                _compiler_limit_error(
                    diagnostics,
                    code="compiler_field_text_limit_exceeded",
                    message="A declarative Skill text field exceeds its bounded character limit.",
                    field=path,
                    limit=max_text,
                    actual=len(node),
                    source_file=source_file,
                )
            return node[:max_text]
        if isinstance(node, (int, float, bool)) or node is None:
            return node
        if len(text) > max_text:
            _compiler_limit_error(
                diagnostics,
                code="compiler_field_text_limit_exceeded",
                message="A declarative Skill value exceeds its bounded character limit.",
                field=path,
                limit=max_text,
                actual=len(text),
                source_file=source_file,
            )
        return text[:max_text]

    return compact(value, depth=0, path=field)


_DECLARED_LOCAL_RESOURCE_KEYS = frozenset(
    {
        "path",
        "paths",
        "file",
        "files",
        "resource",
        "resources",
        "local_resources",
        "templates",
        "assets",
        "scripts",
    }
)
_LOCAL_RESOURCE_SOURCE_NAMES = frozenset({"project", "local", "package"})
_RESOURCE_MAPPING_METADATA_KEYS = frozenset(
    {
        "description",
        "id",
        "kind",
        "label",
        "mime",
        "name",
        "optional",
        "required",
        "source",
        "type",
    }
)
_URL_RESOURCE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _is_external_declared_resource(value: str) -> bool:
    """Return whether a path-shaped declaration names non-package state."""
    text = str(value or "").strip()
    if not text:
        return False
    normalized = text.replace("\\", "/")
    lowered = normalized.casefold()
    if _URL_RESOURCE_RE.match(text) or lowered.startswith(("data:", "urn:")):
        return True
    if lowered.startswith("skill:"):
        return True
    lexical = lowered.removeprefix("./")
    if lexical == "workspace" or lexical.startswith("workspace/"):
        return True
    return normalized.startswith("/") and "/workspace/" in lowered


def _declared_resource_scope(
    mapping: dict[Any, Any],
    inherited_local: bool,
) -> bool:
    """Resolve the nearest explicit source for a structured declaration.

    An omitted source inherits the surrounding package scope.  The three
    portable local aliases are intentionally explicit; every other named
    source belongs to a runtime tool, another Skill, or external state and is
    therefore not attributed to the parent Skill package.
    """
    if "source" in mapping:
        source = mapping.get("source")
        if source is None or (isinstance(source, str) and not source.strip()):
            return inherited_local
        source_name = str(source).strip().casefold()
        return source_name in _LOCAL_RESOURCE_SOURCE_NAMES
    skill = mapping.get("skill")
    if isinstance(skill, str) and skill.strip().casefold().startswith("skill:"):
        return False
    return inherited_local


def _direct_declared_resource_values(
    value: Any,
    *,
    field: str,
) -> list[tuple[str, str]]:
    """Collect direct scalar paths beneath one resource-key value.

    Nested structured mappings are left to the main walker so their own
    ``source`` field remains authoritative.  A simple alias mapping such as
    ``files: {schema: schemas/item.json}`` is supported without interpreting
    descriptive metadata as a path.
    """
    values: list[tuple[str, str]] = []
    stack: list[tuple[Any, str]] = [(value, field)]
    while stack:
        node, path = stack.pop()
        if isinstance(node, str):
            if node.strip():
                values.append((node.strip(), path))
            continue
        if isinstance(node, (list, tuple, set)):
            children = list(node)
            if isinstance(node, set):
                children.sort(key=str)
            for index, child in reversed(list(enumerate(children))):
                if not isinstance(child, dict):
                    stack.append((child, f"{path}[{index}]"))
            continue
        if not isinstance(node, dict):
            continue
        normalized_keys = {str(key).casefold() for key in node}
        if normalized_keys.intersection(_DECLARED_LOCAL_RESOURCE_KEYS):
            # The main declaration walker will preserve source inheritance and
            # consume the nested path/resource keys.
            continue
        for key, child in reversed(list(node.items())):
            key_text = str(key)
            if key_text.casefold() in _RESOURCE_MAPPING_METADATA_KEYS:
                continue
            if isinstance(child, str):
                stack.append((child, f"{path}.{key_text}"))
            elif isinstance(child, (list, tuple, set)) and all(
                isinstance(item, str) for item in child
            ):
                stack.append((child, f"{path}.{key_text}"))
    return values


def _extract_declared_local_resources(
    declaration: Any,
    skill_dir: Path,
    diagnostics: dict[str, list[dict[str, Any]]],
    *,
    field: str,
    source_file: str | None,
) -> list[str]:
    """Compile the safe parent-package file closure named by a declaration.

    Only explicit path/file/resource fields are interpreted.  Runtime
    workspace paths, URLs, and ``skill:*`` references remain external.  A
    declaration that otherwise claims a parent-package file is fail-closed:
    missing, traversing, absolute, non-regular, unreadable, and symlinked
    resources all become compiler errors rather than deferred model guesses.
    """
    candidates: list[tuple[str, str]] = []
    stack: list[tuple[Any, bool, str]] = [(declaration, True, field)]
    visited: set[tuple[int, bool]] = set()
    while stack:
        node, inherited_local, path = stack.pop()
        if isinstance(node, dict):
            identity = (id(node), inherited_local)
            if identity in visited:
                continue
            visited.add(identity)
            local_scope = _declared_resource_scope(node, inherited_local)
            items = list(node.items())
            for key, value in items:
                key_text = str(key)
                if (
                    local_scope
                    and key_text.casefold() in _DECLARED_LOCAL_RESOURCE_KEYS
                ):
                    candidates.extend(
                        _direct_declared_resource_values(
                            value,
                            field=f"{path}.{key_text}",
                        )
                    )
            for key, value in reversed(items):
                if isinstance(value, (dict, list, tuple, set)):
                    stack.append((value, local_scope, f"{path}.{key}"))
            continue
        if isinstance(node, (list, tuple, set)):
            identity = (id(node), inherited_local)
            if identity in visited:
                continue
            visited.add(identity)
            children = list(node)
            if isinstance(node, set):
                children.sort(key=str)
            for index, child in reversed(list(enumerate(children))):
                if isinstance(child, (dict, list, tuple, set)):
                    stack.append((child, inherited_local, f"{path}[{index}]"))

    resources: list[str] = []
    retained_candidates: list[tuple[str, str]] = []
    seen_candidates: set[str] = set()
    for candidate, candidate_field in candidates:
        if candidate in seen_candidates or _is_external_declared_resource(candidate):
            continue
        seen_candidates.add(candidate)
        retained_candidates.append((candidate, candidate_field))
    retained_candidates = [
        (str(candidate), str(candidate_field))
        for candidate, candidate_field in _bounded_sequence(
            retained_candidates,
            limit=MAX_DECLARED_LOCAL_RESOURCES,
            diagnostics=diagnostics,
            field=f"{field}.local_resources",
            source_file=source_file,
        )
    ]
    for candidate, candidate_field in retained_candidates:
        if "\x00" in candidate:
            checked = None
            reason = "invalid_resource_path"
        else:
            try:
                checked = validate_skill_resource(
                    skill_dir,
                    candidate,
                    expected_kind="file",
                    require_relative=True,
                )
                reason = checked.code
            except (OSError, RuntimeError, ValueError):
                checked = None
                reason = "invalid_resource_path"
        if checked is not None and checked.valid and checked.path is not None:
            resources.append(str(checked.path.relative_to(skill_dir)))
            continue
        if reason == "missing_resource":
            code = "missing_declared_local_resource"
            message = "A declarative Skill step references a missing parent-package resource."
        elif reason == "symlink_resource_path":
            code = "symlink_declared_local_resource"
            message = "A declarative Skill step references a symlinked package resource."
        else:
            code = "unsafe_declared_local_resource"
            message = "A declarative Skill step references an unsafe parent-package resource."
        _diagnostic(
            diagnostics,
            "errors",
            code,
            message,
            field=candidate_field,
            source_file=source_file,
            resource=candidate,
            reason=reason,
        )
    return _dedupe(resources)


def _find_duplicate_yaml_keys(text: str) -> list[dict[str, Any]]:
    """Inspect a YAML node tree before construction so overwritten keys are visible."""
    try:
        import yaml
        from yaml.nodes import MappingNode, ScalarNode, SequenceNode

        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
        root = yaml.compose(text, Loader=loader)
    except Exception:
        return []

    duplicates: list[dict[str, Any]] = []
    visited_nodes: set[int] = set()
    stack: list[tuple[Any, list[str], int]] = []
    if root is not None:
        stack.append((root, [], 0))
    inspected = 0
    while stack:
        node, path, depth = stack.pop()
        inspected += 1
        if inspected > MAX_COMPILER_STRUCTURE_NODES:
            break
        if depth > MAX_COMPILER_STRUCTURE_DEPTH:
            continue
        node_identity = id(node)
        if node_identity in visited_nodes:
            continue
        visited_nodes.add(node_identity)
        if isinstance(node, MappingNode):
            seen: set[str] = set()
            children: list[tuple[Any, list[str], int]] = []
            for key_node, value_node in node.value:
                key = (
                    str(key_node.value)
                    if isinstance(key_node, ScalarNode)
                    else "<non-scalar-key>"
                )
                if key in seen:
                    duplicates.append({"key": key, "path": path + [key]})
                seen.add(key)
                children.append((value_node, path + [key], depth + 1))
            stack.extend(reversed(children))
        elif isinstance(node, SequenceNode):
            stack.extend(
                (item, path + [str(index)], depth + 1)
                for index, item in reversed(list(enumerate(node.value)))
            )
    return duplicates


def _load_yaml_mapping(
    path: Path,
    skill_root: Path,
    diagnostics: dict[str, list[dict[str, Any]]],
    *,
    kind: str,
) -> dict[str, Any] | None:
    loader = _get_yaml_loader()
    if not loader:
        _diagnostic(
            diagnostics,
            "warnings",
            "yaml_loader_unavailable",
            "PyYAML is unavailable; structured execution declarations could not be parsed.",
        )
        return None
    checked = validate_skill_resource(skill_root, path, expected_kind="file")
    text = ""
    if checked.valid and checked.path is not None:
        try:
            with checked.path.open("r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(MAX_COMPILER_YAML_SOURCE_CHARS + 1)
        except OSError:
            text = ""
    if not text:
        _diagnostic(
            diagnostics,
            "errors",
            f"unreadable_{kind}_file",
            f"Could not read declared {kind} YAML file.",
            file=str(path),
        )
        return None
    if len(text) > MAX_COMPILER_YAML_SOURCE_CHARS:
        _compiler_limit_error(
            diagnostics,
            code="compiler_yaml_source_limit_exceeded",
            message="A declarative Skill YAML file exceeds the compiler source-text limit.",
            field=f"{kind}_document",
            limit=MAX_COMPILER_YAML_SOURCE_CHARS,
            actual=len(text),
            source_file=str(path),
        )
        return None
    for duplicate in _find_duplicate_yaml_keys(text):
        duplicate_path = [str(part) for part in duplicate.get("path") or []]
        parent_keys = {part.lower() for part in duplicate_path[:-1]}
        if parent_keys.intersection({"routing_rules", "routes"}):
            code = "duplicate_route_id"
            message = "YAML routing declaration repeats a route id; an earlier route would be overwritten."
        elif parent_keys.intersection({"workers", "worker_registry", "agents"}):
            code = "duplicate_worker_id"
            message = "YAML worker registry repeats a worker id; an earlier worker would be overwritten."
        else:
            code = "duplicate_yaml_key"
            message = "YAML mapping repeats a key; an earlier value would be overwritten."
        _diagnostic(
            diagnostics,
            "errors",
            code,
            message,
            file=str(path),
            key=duplicate.get("key"),
            yaml_path=".".join(duplicate_path),
        )
    try:
        data = loader(text)
    except Exception as exc:
        _diagnostic(
            diagnostics,
            "errors",
            f"invalid_{kind}_yaml",
            f"Could not parse {kind} YAML: {exc}",
            file=str(path),
        )
        return None
    if not isinstance(data, dict):
        _diagnostic(
            diagnostics,
            "errors",
            f"invalid_{kind}_document",
            f"Expected the {kind} YAML document to contain a mapping.",
            file=str(path),
        )
        return None
    try:
        relative_file = str(path.relative_to(skill_root))
    except ValueError:
        relative_file = str(path)
    if not _audit_declarative_graph(
        data,
        diagnostics,
        field=f"{kind}_document",
        source_file=relative_file,
    ):
        return None
    return data


_SUPPORTED_ORCHESTRATOR_EXECUTION_FIELDS = frozenset(
    {
        "orchestrator_id",
        "id",
        "name",
        "version",
        "description",
        "workers",
        "worker_registry",
        "agents",
        "routing_rules",
        "routes",
        "route_selection_policy",
        "route_order_policy",
        "routing_policy",
        "route_order",
        "knowledge_bootstrap",
        "aggregation",
        "conflict_resolution",
        "intent_classification",
        "final_report_template",
        "output_contract",
        "quality_contract",
        "allowed-tools",
        "allowed_tools",
        "dependencies",
        "prerequisites",
        "requirements",
        "platforms",
        "required_environment_variables",
    }
)


# These declarations all change *how or when* work executes.  The current
# execution IR has no lossless representation for them at orchestrator scope,
# so accepting the rest of the document while dropping one of these controls
# would execute a materially different workflow.  Keep this vocabulary
# domain-neutral: it describes common workflow engines, not one Skill package.
_UNSUPPORTED_ORCHESTRATOR_EXECUTION_FIELDS = frozenset(
    {
        # Ordered workflow/task DSLs.
        "step",
        "steps",
        "task",
        "tasks",
        "job",
        "jobs",
        "stage",
        "stages",
        "phase",
        "phases",
        "pipeline",
        "workflow",
        # Retry/backoff controls.
        "retry",
        "retries",
        "retry_policy",
        "max_retry",
        "max_retries",
        "retry_count",
        "backoff",
        "backoff_policy",
        "backoff_seconds",
        # Time and scheduling controls.
        "timeout",
        "timeouts",
        "timeout_seconds",
        "deadline",
        "deadline_seconds",
        "schedule",
        "scheduling",
        # Parallelism and failure semantics.
        "concurrency",
        "max_concurrency",
        "parallelism",
        "max_parallelism",
        "on_failure",
        "failure_policy",
        "error_policy",
        "error_handling",
        # Generic execution/dispatch containers.
        "execution",
        "dispatch",
    }
)

_UNSUPPORTED_ORCHESTRATOR_EXECUTION_PREFIXES = (
    "execution_",
    "dispatch_",
    "retry_",
    "timeout_",
    "deadline_",
    "schedule_",
    "scheduling_",
    "concurrency_",
    "parallelism_",
    "failure_",
    "error_handling_",
)

_UNSUPPORTED_ORCHESTRATOR_EXECUTION_SUFFIXES = (
    "_steps",
    "_tasks",
    "_jobs",
    "_stages",
    "_phases",
    "_retry",
    "_retries",
    "_retry_policy",
    "_timeout",
    "_timeout_seconds",
    "_deadline",
    "_deadline_seconds",
    "_concurrency",
    "_parallelism",
    "_failure_policy",
)


def _unsupported_orchestrator_execution_fields(
    data: dict[str, Any],
) -> list[str]:
    """Identify execution-shaped top-level controls with no compiler IR.

    Unknown descriptive extension keys remain forward-compatible.  Only
    conventional control vocabulary is rejected, because silently ignoring
    those fields could change ordering, retries, deadlines, or failure
    behavior.
    """
    unsupported: list[str] = []
    for key in data:
        field = str(key)
        normalized = field.strip().casefold().replace("-", "_")
        if normalized in _SUPPORTED_ORCHESTRATOR_EXECUTION_FIELDS:
            continue
        if (
            normalized in _UNSUPPORTED_ORCHESTRATOR_EXECUTION_FIELDS
            or normalized.startswith(
                _UNSUPPORTED_ORCHESTRATOR_EXECUTION_PREFIXES
            )
            or normalized.endswith(
                _UNSUPPORTED_ORCHESTRATOR_EXECUTION_SUFFIXES
            )
            or normalized
            in {
                "workflow_graph",
                "workflow_dag",
                "workflow_plan",
                "task_graph",
                "task_dag",
                "task_plan",
                "agent_graph",
                "agent_dag",
                "agent_plan",
                "worker_graph",
                "worker_dag",
                "worker_plan",
            }
        ):
            unsupported.append(field)
    return unsupported


def _looks_like_orchestrator(data: dict[str, Any]) -> bool:
    signals = {
        "orchestrator_id",
        "workers",
        "worker_registry",
        "agents",
        "routing_rules",
        "routes",
        "knowledge_bootstrap",
        "aggregation",
        "conflict_resolution",
        "final_report_template",
        "output_contract",
        "quality_contract",
        "allowed-tools",
        "allowed_tools",
        "dependencies",
        "prerequisites",
        "requirements",
        "platforms",
        "required_environment_variables",
    }
    return bool(
        signals.intersection(data)
        or _unsupported_orchestrator_execution_fields(data)
    )


def _worker_entries_from_registry(value: Any) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(value, dict):
        for worker_id, config in value.items():
            if isinstance(config, dict):
                entries.append((str(worker_id), config))
            elif isinstance(config, str):
                entries.append((str(worker_id), {"file": config}))
    elif isinstance(value, list):
        for index, config in enumerate(value):
            if isinstance(config, str):
                if "/" in config or Path(config).suffix.lower() in {".yaml", ".yml", ".md", ".json"}:
                    entries.append((_worker_id_from_path(config), {"file": config}))
                else:
                    entries.append((config, {"worker_id": config}))
            elif isinstance(config, dict):
                worker_id = (
                    config.get("worker_id")
                    or config.get("id")
                    or config.get("name")
                    or f"worker-{index + 1}"
                )
                entries.append((str(worker_id), config))
    return entries


def _is_explicit_worker_declaration(config: dict[str, Any]) -> bool:
    """Return whether a standalone package file declares executable work."""
    if str(config.get("worker_id") or "").strip():
        return True
    if config.get("worker") is True:
        return True
    for key in ("kind", "type", "agent_type", "declaration"):
        value = str(config.get(key) or "").strip().casefold().replace("-", "_")
        if value in {"worker", "worker_agent", "subagent", "sub_agent"}:
            return True
    metadata = config.get("metadata")
    if isinstance(metadata, dict):
        value = str(metadata.get("kind") or metadata.get("type") or "").strip().casefold()
        if value in {"worker", "worker-agent", "worker_agent", "subagent"}:
            return True
    return False


def _registered_worker_resource_paths(
    skill_dir: Path,
    orchestrators: list[tuple[str, dict[str, Any]]],
) -> set[str]:
    """Collect safe worker files whose identity is owned by a registry."""
    paths: set[str] = set()
    for _, orchestrator in orchestrators:
        for registry_key in ("workers", "worker_registry", "agents"):
            for _, config in _worker_entries_from_registry(orchestrator.get(registry_key)):
                file_value = config.get("file") or config.get("path") or config.get("config")
                if not file_value:
                    continue
                checked = validate_skill_resource(
                    skill_dir,
                    str(file_value).strip(),
                    expected_kind="file",
                    require_relative=True,
                )
                if checked.valid and checked.path is not None:
                    paths.add(str(checked.path.relative_to(skill_dir)))
    return paths


def _normalize_worker_config(
    worker_id: str,
    config: dict[str, Any],
    *,
    skill_dir: Path,
    file_path: str | None,
    diagnostics: dict[str, list[dict[str, Any]]],
    source_file: str | None,
) -> dict[str, Any]:
    dependencies = _as_string_list(
        config.get("depends_on")
        or config.get("dependencies")
        or config.get("requires_workers")
    )
    knowledge_gate = config.get("knowledge_gate")
    gate_checks = knowledge_gate.get("checks") if isinstance(knowledge_gate, dict) else []
    if isinstance(gate_checks, dict):
        gate_checks = [
            dict(value, id=key) if isinstance(value, dict) else {"id": key, "description": value}
            for key, value in gate_checks.items()
        ]
    if not isinstance(gate_checks, list):
        gate_checks = []
    required_gate_ids = [
        str(check.get("id") or check.get("name")).strip()
        for check in gate_checks
        if isinstance(check, dict) and (check.get("id") or check.get("name"))
    ]
    dependencies = [str(value) for value in _bounded_sequence(
        dependencies,
        limit=80,
        diagnostics=diagnostics,
        field=f"workers[{worker_id}].dependencies",
        source_file=source_file,
    )]
    required_gate_ids = [str(value) for value in _bounded_sequence(
        required_gate_ids,
        limit=80,
        diagnostics=diagnostics,
        field=f"workers[{worker_id}].required_gate_ids",
        source_file=source_file,
    )]
    local_resource_declaration: dict[str, Any] = {
        key: config.get(key)
        for key in ("tools", "capabilities", "skills")
        if config.get(key) is not None
    }
    for key in ("path", "paths", "file", "files", "resource", "resources"):
        if config.get(key) is None:
            continue
        # Registry ``file``/``path`` fields point at the worker declaration
        # itself.  ``file_path`` is already an independently validated worker
        # resource, so do not mislabel that structural pointer as a tool input.
        if (
            key in {"file", "path"}
            and file_path
            and str(config.get(key)).strip() == file_path
        ):
            continue
        local_resource_declaration[key] = config.get(key)
    local_resources = _extract_declared_local_resources(
        local_resource_declaration,
        skill_dir,
        diagnostics,
        field=f"workers[{worker_id}]",
        source_file=source_file,
    )
    environment_contract = _compile_nested_environment_contract(
        config,
        source_file=source_file,
        diagnostics=diagnostics,
        include_dependencies=False,
    )
    worker = {
        "id": str(config.get("worker_id") or config.get("id") or worker_id).strip(),
        "file": file_path,
        "name": _bounded_text(
            config.get("name"),
            limit=240,
            diagnostics=diagnostics,
            field=f"workers[{worker_id}].name",
            source_file=source_file,
        ),
        "role_hint": _bounded_text(
            config.get("role")
            or config.get("role_hint")
            or config.get("objective")
            or config.get("mission")
            or config.get("name")
            or "",
            limit=240,
            diagnostics=diagnostics,
            field=f"workers[{worker_id}].role_hint",
            source_file=source_file,
        ),
        "version": str(config.get("version") or "").strip(),
        "dependencies": dependencies,
        "required_gate_ids": required_gate_ids,
        "knowledge_gate": _compact_mapping(
            knowledge_gate,
            max_items=80,
            max_text=2_000,
            diagnostics=diagnostics,
            field=f"workers[{worker_id}].knowledge_gate",
            source_file=source_file,
        ),
        "tools": _compact_mapping(
            config.get("tools"),
            max_items=80,
            max_text=1_000,
            diagnostics=diagnostics,
            field=f"workers[{worker_id}].tools",
            source_file=source_file,
        ),
        "capabilities": _compact_mapping(
            config.get("capabilities"),
            max_items=80,
            max_text=1_000,
            diagnostics=diagnostics,
            field=f"workers[{worker_id}].capabilities",
            source_file=source_file,
        ),
        "skills": _compact_mapping(
            config.get("skills"),
            max_items=80,
            max_text=1_000,
            diagnostics=diagnostics,
            field=f"workers[{worker_id}].skills",
            source_file=source_file,
        ),
        "local_resources": local_resources,
        "environment_contract": environment_contract,
        "output_schema": _compact_mapping(
            config.get("output_schema") or config.get("output_format"),
            max_items=80,
            max_text=1_500,
            diagnostics=diagnostics,
            field=f"workers[{worker_id}].output_schema",
            source_file=source_file,
        ),
    }
    return {key: value for key, value in worker.items() if value not in ("", None, [], {})}


def _discover_declared_workers(
    skill_dir: Path,
    worker_files: list[str],
    orchestrators: list[tuple[str, dict[str, Any]]],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    workers_by_id: dict[str, dict[str, Any]] = {}
    registry_declarations: dict[str, tuple[str, str | None]] = {}
    registered_worker_files = _registered_worker_resource_paths(
        skill_dir,
        orchestrators,
    )

    for relative_path in worker_files:
        checked = validate_skill_resource(
            skill_dir,
            relative_path,
            expected_kind="file",
            require_relative=True,
        )
        if not checked.valid or checked.path is None:
            _diagnostic(
                diagnostics,
                "errors",
                "unsafe_worker_file",
                "Discovered worker resource failed the Skill package boundary check.",
                file=relative_path,
                reason=checked.code,
            )
            continue
        path = checked.path
        relative_path = str(path.relative_to(skill_dir))
        if path.suffix.lower() not in {".yaml", ".yml", ".md"}:
            continue
        if relative_path in registered_worker_files:
            continue
        config: dict[str, Any] = {}
        if path.suffix.lower() in {".yaml", ".yml"}:
            parsed = _load_yaml_mapping(path, skill_dir, diagnostics, kind="worker")
            if parsed is None:
                continue
            config = parsed
        elif path.suffix.lower() == ".md":
            raw, _, source_truncated, source_unreadable = (
                _read_semantic_text_resource(path, skill_dir)
            )
            if source_truncated or source_unreadable:
                # The semantic closure emits the authoritative diagnostic.
                # Never reinterpret a bounded prefix as a complete worker
                # declaration.
                continue
            try:
                frontmatter, _ = parse_frontmatter(raw, strict=True)
            except FrontmatterParseError as exc:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_worker_frontmatter",
                    "A standalone worker Markdown file has invalid YAML frontmatter.",
                    file=relative_path,
                    reason=exc.code,
                    detail=str(exc),
                    **exc.context,
                )
                continue
            config = frontmatter
            if (
                path.stem.lower() in {"readme", "index", "overview", "template"}
                and not (config.get("worker_id") or config.get("id"))
            ):
                continue
        # A registry owns both identity and configuration overlay for its
        # referenced file; it is loaded in the registry pass below.  All other
        # files in a workers-looking directory are executable only when their
        # own metadata explicitly says so.  This keeps examples, schemas, and
        # templates from silently becoming agents because of their filename.
        if not _is_explicit_worker_declaration(config):
            continue
        worker_id = str(
            config.get("worker_id")
            or config.get("id")
            or _worker_id_from_path(relative_path)
        ).strip()
        worker = _normalize_worker_config(
            worker_id,
            config,
            skill_dir=skill_dir,
            file_path=relative_path,
            diagnostics=diagnostics,
            source_file=relative_path,
        )
        if not worker.get("name"):
            role_hint = _worker_role_hint(path, skill_dir)
            if role_hint:
                worker["role_hint"] = role_hint
        if worker_id in workers_by_id:
            _diagnostic(
                diagnostics,
                "errors",
                "duplicate_worker_id",
                f"Worker id {worker_id!r} is declared by more than one file.",
                worker_id=worker_id,
                file=relative_path,
                previous_file=workers_by_id[worker_id].get("file"),
            )
            continue
        workers_by_id[worker_id] = worker

    for source_file, orchestrator in orchestrators:
        for registry_key in ("workers", "worker_registry", "agents"):
            for declared_id, config in _worker_entries_from_registry(orchestrator.get(registry_key)):
                file_value = config.get("file") or config.get("path") or config.get("config")
                relative_path = str(file_value).strip() if file_value else None
                previous_registry = registry_declarations.get(declared_id)
                if previous_registry is not None:
                    _diagnostic(
                        diagnostics,
                        "errors",
                        "duplicate_worker_id",
                        f"Worker id {declared_id!r} is repeated in orchestrator registries.",
                        worker_id=declared_id,
                        orchestrator_file=source_file,
                        file=relative_path,
                        previous_orchestrator_file=previous_registry[0],
                        previous_file=previous_registry[1],
                    )
                else:
                    registry_declarations[declared_id] = (source_file, relative_path)
                configured_id = str(
                    config.get("worker_id") or config.get("id") or declared_id
                ).strip()
                if configured_id != declared_id:
                    _diagnostic(
                        diagnostics,
                        "warnings",
                        "worker_registry_id_mismatch",
                        "Worker registry key and embedded worker id differ.",
                        worker_id=declared_id,
                        configured_worker_id=configured_id,
                        orchestrator_file=source_file,
                    )
                if relative_path:
                    checked = validate_skill_resource(
                        skill_dir,
                        relative_path,
                        expected_kind="file",
                        require_relative=True,
                    )
                    if not checked.valid or checked.path is None:
                        if checked.code == "missing_resource":
                            code = "missing_worker_file"
                            message = (
                                f"Worker {declared_id!r} references a file that does not exist."
                            )
                        elif checked.code == "symlink_resource_path":
                            code = "symlink_worker_file_reference"
                            message = (
                                f"Worker {declared_id!r} references a symlinked package resource."
                            )
                        else:
                            code = "unsafe_worker_file_reference"
                            message = (
                                f"Worker {declared_id!r} references a path outside the safe Skill package boundary."
                            )
                        _diagnostic(
                            diagnostics,
                            "errors",
                            code,
                            message,
                            worker_id=declared_id,
                            file=relative_path,
                            orchestrator_file=source_file,
                            reason=checked.code,
                        )
                        continue
                    relative_path = str(checked.path.relative_to(skill_dir))
                file_config: dict[str, Any] = {}
                if relative_path:
                    worker_path = skill_dir / relative_path
                    if worker_path.suffix.lower() in {".yaml", ".yml"}:
                        parsed_file_config = _load_yaml_mapping(
                            worker_path,
                            skill_dir,
                            diagnostics,
                            kind="worker",
                        )
                        if parsed_file_config is None:
                            continue
                        file_config = parsed_file_config
                    elif worker_path.suffix.lower() == ".md":
                        raw, _, source_truncated, source_unreadable = (
                            _read_semantic_text_resource(worker_path, skill_dir)
                        )
                        if source_truncated or source_unreadable:
                            # The package semantic scan has already made this
                            # a fail-closed diagnostic; do not compile a
                            # partial registered-worker declaration.
                            continue
                        try:
                            file_frontmatter, _ = parse_frontmatter(
                                raw,
                                strict=True,
                            )
                        except FrontmatterParseError as exc:
                            _diagnostic(
                                diagnostics,
                                "errors",
                                "invalid_worker_frontmatter",
                                "A registered worker Markdown file has invalid YAML frontmatter.",
                                file=relative_path,
                                reason=exc.code,
                                detail=str(exc),
                                **exc.context,
                            )
                            continue
                        file_config = file_frontmatter
                declared_file_id = str(
                    file_config.get("worker_id") or file_config.get("id") or ""
                ).strip()
                if declared_file_id and declared_file_id != declared_id:
                    _diagnostic(
                        diagnostics,
                        "warnings",
                        "worker_registry_id_mismatch",
                        "Worker registry key and referenced file worker id differ; the registry id is authoritative.",
                        worker_id=declared_id,
                        configured_worker_id=declared_file_id,
                        orchestrator_file=source_file,
                        file=relative_path,
                    )
                merged_config = dict(file_config)
                merged_config.update(config)
                merged_config["worker_id"] = declared_id
                normalized = _normalize_worker_config(
                    declared_id,
                    merged_config,
                    skill_dir=skill_dir,
                    file_path=relative_path,
                    diagnostics=diagnostics,
                    source_file=source_file,
                )
                existing = workers_by_id.get(declared_id)
                if existing:
                    if normalized.get("local_resources"):
                        existing["local_resources"] = _dedupe(
                            list(existing.get("local_resources") or [])
                            + list(normalized["local_resources"])
                        )
                    for key, value in normalized.items():
                        existing.setdefault(key, value)
                else:
                    workers_by_id[declared_id] = normalized

    workers = list(workers_by_id.values())
    return [
        worker
        for worker in _bounded_sequence(
            workers,
            limit=MAX_COMPILED_WORKERS,
            diagnostics=diagnostics,
            field="workers",
        )
        if isinstance(worker, dict)
    ]


_ROUTE_SIGNAL_KEYS = {
    "pattern",
    "patterns",
    "regex",
    "match",
    "when",
    "worker",
    "workers",
    "parallel_workers",
    "sequential_workers",
    "spawn_mode",
    "stages",
    "waves",
}


_ROUTE_ARTIFACT_PATH_KEYS = (
    "canonical",
    "path",
    "filepath",
    "file",
    "filename",
    "output_artifact",
)
_ROUTE_ARTIFACT_LIST_KEYS = ("artifacts", "files", "outputs")


def _safe_structured_artifact_path(value: str) -> bool:
    """Accept any safe exact workspace-relative POSIX path.

    This deliberately imposes no filename numbering convention and no
    extension allow-list: Unicode paths plus Markdown/JSON/CSV/YAML and other
    exact deliverables are all valid when a structured contract declares them.
    """
    path = value.strip()
    if not path or "\x00" in path or "\\" in path:
        return False
    if re.match(r"^[A-Za-z]:", path) or re.match(r"^[a-z][a-z0-9+.-]*:", path, re.I):
        return False
    parsed = PurePosixPath(path)
    return (
        not parsed.is_absolute()
        and all(part not in {"", ".", ".."} for part in parsed.parts)
    )


def _normalize_structured_sections(
    value: Any,
    *,
    source_file: str,
    diagnostics: dict[str, list[dict[str, Any]]],
    field: str = "output_contract.sections",
) -> list[dict[str, Any]]:
    """Normalize declarative output sections independently of report templates.

    ``final_report_template.sections`` is a legacy-compatible input shape, not
    the canonical contract.  Standard structured Skills may declare the same
    section metadata directly under ``output_contract.sections``.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        _diagnostic(
            diagnostics,
            "errors",
            "invalid_output_sections",
            "output_contract.sections must be an ordered list.",
            field=field,
            source_file=source_file,
        )
        return []
    bounded = _bounded_sequence(
        value,
        limit=60,
        diagnostics=diagnostics,
        field=field,
        source_file=source_file,
    )
    normalized: list[dict[str, Any]] = []
    for index, section in enumerate(bounded):
        section_field = f"{field}[{index}]"
        if isinstance(section, str) and section.strip():
            title = section.strip()
            normalized.append(
                {
                    "id": title,
                    "title": title,
                    "order": index + 1,
                    "source_file": source_file,
                }
            )
            continue
        if not isinstance(section, dict):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_output_section",
                "Each output section must be a non-empty string or mapping.",
                field=section_field,
                source_file=source_file,
            )
            continue
        title = (
            section.get("section")
            or section.get("title")
            or section.get("name")
            or section.get("id")
        )
        if not isinstance(title, str) or not title.strip():
            _diagnostic(
                diagnostics,
                "errors",
                "missing_output_section_id",
                "Each output section needs an id, title, name, or section label.",
                field=section_field,
                source_file=source_file,
            )
            continue
        section_id = str(section.get("id") or title).strip()
        order = _parse_int_literal(section.get("order")) or index + 1
        normalized_section: dict[str, Any] = {
            "id": section_id,
            "title": title.strip(),
            "order": order,
            "source_workers": _as_string_list(
                section.get("source_workers") or section.get("source_worker")
            ),
            "content": _bounded_text(
                section.get("content"),
                limit=1_000,
                diagnostics=diagnostics,
                field=f"{section_field}.content",
                source_file=source_file,
            ),
            "key_elements": _compact_mapping(
                section.get("key_elements"),
                max_items=40,
                diagnostics=diagnostics,
                field=f"{section_field}.key_elements",
                source_file=source_file,
            ),
            "source_file": source_file,
        }
        applicability = section.get("applicability")
        if applicability is not None:
            if isinstance(applicability, str):
                normalized_section["applicability"] = _bounded_text(
                    applicability,
                    limit=4_096,
                    diagnostics=diagnostics,
                    field=f"{section_field}.applicability",
                    source_file=source_file,
                )
            else:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_section_applicability",
                    "Section applicability must be descriptive text; executable conditions require an explicit supported predicate schema.",
                    field=f"{section_field}.applicability",
                    source_file=source_file,
                )
        normalized.append(
            {
                key: item
                for key, item in normalized_section.items()
                if item not in ("", None, [], {})
            }
        )
    return normalized


def _normalize_structured_package_output_contract(
    value: Any,
    *,
    skill_dir: Path,
    source_file: str,
    diagnostics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Normalize an explicit package-level output contract.

    Structured declarations are authoritative and format-agnostic.  Legacy
    Markdown prose extraction remains a fallback for older Skills.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        _diagnostic(
            diagnostics,
            "errors",
            "invalid_package_output_contract",
            "Package output_contract must be a mapping.",
            source_file=source_file,
        )
        return {}

    normalized: dict[str, Any] = {}
    inferred_formats: dict[str, str] = {}

    def normalize_paths(raw: Any, field: str) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_package_artifact_list",
                "Structured package artifact declarations must be ordered lists.",
                field=field,
                source_file=source_file,
            )
            return []
        paths: list[str] = []
        for index, item in enumerate(raw[:80]):
            raw_path: Any = item
            raw_format: Any = None
            if isinstance(item, dict):
                path_values = [
                    item.get(key)
                    for key in _ROUTE_ARTIFACT_PATH_KEYS
                    if item.get(key) is not None
                ]
                candidates = _dedupe([
                    str(candidate).strip()
                    for candidate in path_values
                    if isinstance(candidate, str) and candidate.strip()
                ])
                raw_path = candidates[0] if len(candidates) == 1 else None
                raw_format = (
                    item.get("format")
                    if item.get("format") is not None
                    else item.get("type")
                )
            if not isinstance(raw_path, str) or not raw_path.strip():
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_package_artifact_path",
                    "Each structured package artifact needs one exact path.",
                    field=f"{field}[{index}]",
                    source_file=source_file,
                )
                continue
            path = raw_path.strip()
            if not _safe_structured_artifact_path(path):
                _diagnostic(
                    diagnostics,
                    "errors",
                    "unsafe_package_artifact_path",
                    "A structured package artifact path must be workspace-relative and safe.",
                    field=f"{field}[{index}]",
                    artifact=path,
                    source_file=source_file,
                )
                continue
            paths.append(path)
            if isinstance(raw_format, str) and raw_format.strip():
                inferred_formats[path] = raw_format.strip().casefold()
        if len(raw) > 80:
            _compiler_limit_error(
                diagnostics,
                code="compiler_field_item_limit_exceeded",
                message="Package artifact declaration exceeds its bounded item limit.",
                field=field,
                limit=80,
                actual=len(raw),
                source_file=source_file,
            )
        return _dedupe(paths)

    path_list_aliases = {
        "declared_artifacts": ("declared_artifacts", "artifacts", "outputs"),
        "declared_modular_files": ("declared_modular_files", "modules"),
        "declared_ancillary_files": ("declared_ancillary_files", "ancillary"),
        "merge_input_order": ("merge_input_order", "merge_inputs"),
    }
    for canonical, aliases in path_list_aliases.items():
        declared_alias = next((alias for alias in aliases if alias in value), None)
        raw = value.get(declared_alias) if declared_alias is not None else None
        paths = normalize_paths(raw, f"output_contract.{canonical}")
        if declared_alias is not None:
            # Presence is authoritative even when the ordered list is empty.
            # This prevents a final-only structured package from being widened
            # by stale legacy format examples during the later fallback pass.
            normalized[canonical] = paths

    final_value = next(
        (
            value.get(alias)
            for alias in (
                "declared_final_artifact",
                "final_artifact",
                "output_artifact",
            )
            if alias in value
        ),
        None,
    )
    if final_value is not None:
        if (
            isinstance(final_value, str)
            and _safe_structured_artifact_path(final_value.strip())
        ):
            normalized["declared_final_artifact"] = final_value.strip()
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "unsafe_package_final_artifact_path",
                "The structured final artifact must be one safe workspace-relative path.",
                source_file=source_file,
            )

    for field_name in (
        "declared_file_count",
        "declared_modular_file_count",
        "declared_section_count",
        "expected_min_bytes",
        "expected_max_bytes",
        "expected_min_lines",
        "expected_max_lines",
    ):
        raw = value.get(field_name)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            normalized[field_name] = raw
        elif field_name in value:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_package_output_contract_field",
                f"output_contract.{field_name} must be a non-negative integer.",
                field=f"output_contract.{field_name}",
                source_file=source_file,
            )

    if isinstance(value.get("merge_mandatory"), bool):
        normalized["merge_mandatory"] = value["merge_mandatory"]
    elif "merge_mandatory" in value:
        _diagnostic(
            diagnostics,
            "errors",
            "invalid_package_output_contract_field",
            "output_contract.merge_mandatory must be a boolean.",
            field="output_contract.merge_mandatory",
            source_file=source_file,
        )
    for field_name in (
        "readme_is_index",
        "enforce_exact_section_titles",
        "verify_first_last_match",
    ):
        if field_name not in value:
            continue
        raw = value.get(field_name)
        if isinstance(raw, bool):
            normalized[field_name] = raw
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_package_output_contract_field",
                f"output_contract.{field_name} must be a boolean.",
                field=f"output_contract.{field_name}",
                source_file=source_file,
            )
    for field_name in ("merge_separator", "merge_command"):
        raw = value.get(field_name)
        if isinstance(raw, str):
            normalized[field_name] = raw
        elif field_name in value:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_package_output_contract_field",
                f"output_contract.{field_name} must be a string.",
                field=f"output_contract.{field_name}",
                source_file=source_file,
            )

    if "required_markers" in value:
        raw_required_markers = value.get("required_markers")
        if not isinstance(raw_required_markers, list) or not all(
            isinstance(item, str) and item.strip()
            for item in raw_required_markers
        ):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_package_output_contract_field",
                "output_contract.required_markers must be an ordered list of non-empty strings.",
                field="output_contract.required_markers",
                source_file=source_file,
            )
        else:
            normalized["required_markers"] = _dedupe([
                str(item).strip()
                for item in _bounded_sequence(
                    raw_required_markers,
                    limit=64,
                    diagnostics=diagnostics,
                    field="output_contract.required_markers",
                    source_file=source_file,
                )
            ])

    if "sections" in value:
        normalized["sections"] = _normalize_structured_sections(
            value.get("sections"),
            source_file=source_file,
            diagnostics=diagnostics,
        )

    raw_post_merge_checks = value.get("post_merge_checks")
    if "post_merge_checks" in value:
        if not isinstance(raw_post_merge_checks, list):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_post_merge_checks",
                "output_contract.post_merge_checks must be an ordered string list.",
                source_file=source_file,
            )
        else:
            normalized["post_merge_checks"] = [
                str(item).strip()
                for item in _bounded_sequence(
                    raw_post_merge_checks,
                    limit=20,
                    diagnostics=diagnostics,
                    field="output_contract.post_merge_checks",
                    source_file=source_file,
                )
                if isinstance(item, str) and str(item).strip()
            ]

    # A nested merge mapping is a format-agnostic canonical declaration.  The
    # flat fields remain the runtime IR so existing Skills and the deterministic
    # ArtifactPlan share exactly one execution path.
    raw_merge = value.get("merge")
    if "merge" in value and not isinstance(raw_merge, dict):
        _diagnostic(
            diagnostics,
            "errors",
            "invalid_package_merge_contract",
            "output_contract.merge must be a mapping.",
            source_file=source_file,
        )
    elif isinstance(raw_merge, dict):
        if isinstance(raw_merge.get("mandatory"), bool):
            normalized["merge_mandatory"] = raw_merge["mandatory"]
        merge_inputs = normalize_paths(
            raw_merge.get("input_order")
            if "input_order" in raw_merge
            else raw_merge.get("inputs"),
            "output_contract.merge.input_order",
        )
        if "input_order" in raw_merge or "inputs" in raw_merge:
            normalized["merge_input_order"] = merge_inputs
        for source_key, target_key in (
            ("separator", "merge_separator"),
            ("command", "merge_command"),
        ):
            raw = raw_merge.get(source_key)
            if raw is not None:
                if isinstance(raw, str):
                    normalized[target_key] = raw
                else:
                    _diagnostic(
                        diagnostics,
                        "errors",
                        "invalid_package_merge_field",
                        f"output_contract.merge.{source_key} must be a string.",
                        field=f"output_contract.merge.{source_key}",
                        source_file=source_file,
                    )
        merge_output = raw_merge.get("output") or raw_merge.get("output_artifact")
        if merge_output is not None:
            if (
                isinstance(merge_output, str)
                and _safe_structured_artifact_path(merge_output.strip())
            ):
                normalized["declared_final_artifact"] = merge_output.strip()
            else:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "unsafe_package_final_artifact_path",
                    "output_contract.merge.output must be one safe workspace-relative path.",
                    field="output_contract.merge.output",
                    source_file=source_file,
                )
        merge_checks = raw_merge.get("post_merge_checks")
        if merge_checks is not None:
            if not isinstance(merge_checks, list):
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_post_merge_checks",
                    "output_contract.merge.post_merge_checks must be an ordered string list.",
                    source_file=source_file,
                )
            else:
                normalized["post_merge_checks"] = [
                    str(item).strip()
                    for item in _bounded_sequence(
                        merge_checks,
                        limit=20,
                        diagnostics=diagnostics,
                        field="output_contract.merge.post_merge_checks",
                        source_file=source_file,
                    )
                    if isinstance(item, str) and str(item).strip()
                ]
        normalized["merge_declarations"] = [{
            key: item
            for key, item in {
                "mandatory": normalized.get("merge_mandatory"),
                "input_order": normalized.get("merge_input_order"),
                "separator": normalized.get("merge_separator"),
                "command": normalized.get("merge_command"),
                "output_artifact": normalized.get("declared_final_artifact"),
                "post_merge_checks": normalized.get("post_merge_checks"),
                "source_file": source_file,
            }.items()
            if item not in (None, [], {})
        }]

    raw_formats = value.get("artifact_formats")
    if "artifact_formats" in value and not isinstance(raw_formats, dict):
        _diagnostic(
            diagnostics,
            "errors",
            "invalid_package_artifact_formats",
            "artifact_formats must be a mapping of artifact paths to format names.",
            source_file=source_file,
        )
    elif isinstance(raw_formats, dict):
        for raw_path, raw_format in list(raw_formats.items())[:80]:
            if (
                isinstance(raw_path, str)
                and _safe_structured_artifact_path(raw_path.strip())
                and isinstance(raw_format, str)
                and raw_format.strip()
            ):
                inferred_formats[raw_path.strip()] = raw_format.strip().casefold()
            else:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_package_artifact_format",
                    "artifact_formats entries need a safe path and non-empty format.",
                    source_file=source_file,
                )
        if len(raw_formats) > 80:
            _compiler_limit_error(
                diagnostics,
                code="compiler_field_item_limit_exceeded",
                message="artifact_formats exceeds its bounded item limit.",
                field="output_contract.artifact_formats",
                limit=80,
                actual=len(raw_formats),
                source_file=source_file,
            )
    if inferred_formats:
        normalized["artifact_formats"] = inferred_formats

    raw_validators = value.get("artifact_validators")
    if "artifact_validators" in value and not isinstance(raw_validators, dict):
        _diagnostic(
            diagnostics,
            "errors",
            "invalid_package_artifact_validators",
            "artifact_validators must map declared artifact paths to data-only validator specifications.",
            source_file=source_file,
        )
    elif isinstance(raw_validators, dict):
        normalized_validators: dict[str, Any] = {}
        for index, (raw_path, raw_specification) in enumerate(
            list(raw_validators.items())[:256]
        ):
            if not (
                isinstance(raw_path, str)
                and _safe_structured_artifact_path(raw_path.strip())
            ):
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_package_artifact_validator_path",
                    "Each artifact validator needs one safe workspace-relative declared path.",
                    field=f"output_contract.artifact_validators[{index}]",
                    source_file=source_file,
                )
                continue
            if not isinstance(raw_specification, (str, dict)):
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_package_artifact_validator_specification",
                    "An artifact validator must be a format string or a data-only mapping.",
                    field=f"output_contract.artifact_validators.{raw_path}",
                    source_file=source_file,
                )
                continue
            normalized_validators[raw_path.strip()] = _compact_mapping(
                raw_specification,
                max_items=40,
                max_text=2_000,
                diagnostics=diagnostics,
                field=f"output_contract.artifact_validators.{raw_path}",
                source_file=source_file,
            )
        if len(raw_validators) > 256:
            _compiler_limit_error(
                diagnostics,
                code="compiler_field_item_limit_exceeded",
                message="artifact_validators exceeds its bounded item limit.",
                field="output_contract.artifact_validators",
                limit=256,
                actual=len(raw_validators),
                source_file=source_file,
            )
        if normalized_validators:
            normalized["artifact_validators"] = normalized_validators

    declared_output_resources = {
        key: value[key]
        for key in (
            "local_resources",
            "resources",
            "templates",
            "assets",
            "scripts",
        )
        if key in value
    }
    local_resources = _extract_declared_local_resources(
        declared_output_resources,
        skill_dir,
        diagnostics,
        field="output_contract",
        source_file=source_file,
    )
    if local_resources:
        normalized["local_resources"] = local_resources

    policy = value.get("artifact_set_policy")
    if "artifact_set_policy" in value and not isinstance(policy, dict):
        _diagnostic(
            diagnostics,
            "errors",
            "invalid_artifact_set_policy",
            "artifact_set_policy must be a mapping.",
            source_file=source_file,
        )
    elif isinstance(policy, dict):
        normalized_policy: dict[str, Any] = {}
        mode = policy.get("mode")
        if "mode" in policy and not (
            isinstance(mode, str) and mode.strip()
        ):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_artifact_set_policy_mode",
                "artifact_set_policy.mode must be a non-empty string.",
                source_file=source_file,
                actual=mode,
            )
        elif isinstance(mode, str) and mode.strip():
            normalized_mode = mode.strip().casefold().replace("-", "_")
            mode_aliases = {
                "exact": "exact",
                "open": "open",
                "allow_additional": "open",
                "non_exact": "open",
            }
            canonical_mode = mode_aliases.get(normalized_mode)
            if canonical_mode is None:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_artifact_set_policy_mode",
                    "artifact_set_policy.mode must be exact or open.",
                    source_file=source_file,
                    actual=mode,
                )
            else:
                normalized_policy["mode"] = canonical_mode
        for field_name in ("artifacts", "allowed_additional_patterns"):
            paths = normalize_paths(
                policy.get(field_name),
                f"output_contract.artifact_set_policy.{field_name}",
            )
            if paths:
                normalized_policy[field_name] = paths
        if normalized_policy:
            normalized["artifact_set_policy"] = normalized_policy

    index = value.get("artifact_index")
    if isinstance(index, str) and _safe_structured_artifact_path(index.strip()):
        normalized["artifact_index"] = index.strip()
    elif isinstance(index, dict):
        index_file = index.get("file") or index.get("path")
        if (
            isinstance(index_file, str)
            and _safe_structured_artifact_path(index_file.strip())
        ):
            normalized["artifact_index"] = {
                "file": index_file.strip(),
                **(
                    {"coverage_mode": str(index["coverage_mode"]).strip()}
                    if index.get("coverage_mode") is not None
                    else {}
                ),
            }
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_package_artifact_index",
                "artifact_index mapping must contain one safe workspace-relative file/path.",
                field="output_contract.artifact_index",
                source_file=source_file,
            )
    elif "artifact_index" in value:
        _diagnostic(
            diagnostics,
            "errors",
            "invalid_package_artifact_index",
            "artifact_index must be one safe path string or path-bearing mapping.",
            field="output_contract.artifact_index",
            source_file=source_file,
        )
    return normalized


def _normalize_route_output_contract(
    route: dict[str, Any],
    *,
    route_id: str,
    source_file: str,
    diagnostics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Compile only exact route-scoped artifact declarations.

    A route-level ``deliverable``/``output`` is independent of the package-wide
    report template.  The compiler accepts an explicit path-bearing mapping (or
    a list under an explicit artifact-list key); it never derives a filename or
    a format from prose, the route id, or a file suffix.
    """

    raw_declarations: list[tuple[str, Any]] = []
    for field in ("deliverable", "output"):
        value = route.get(field)
        if value is not None:
            raw_declarations.append((field, value))
    if not raw_declarations:
        return {}

    artifacts: list[dict[str, str]] = []

    def add_artifact(value: Any, field: str) -> None:
        if isinstance(value, str):
            # A scalar nested under ``artifacts``/``files`` is itself an exact
            # declaration.  Top-level scalar output prose is intentionally not
            # routed here.
            path = value.strip()
            if path and _safe_structured_artifact_path(path):
                artifacts.append({"path": path})
            elif path:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "unsafe_route_artifact_path",
                    "A structured artifact path must be a safe workspace-relative path.",
                    route_id=route_id,
                    field=field,
                    artifact=path,
                    orchestrator_file=source_file,
                )
            return
        if not isinstance(value, dict):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_route_artifact_declaration",
                "Route artifact declarations must be path-bearing mappings or exact path strings in an artifact list.",
                route_id=route_id,
                field=field,
                orchestrator_file=source_file,
            )
            return
        path_values = [
            value.get(key)
            for key in _ROUTE_ARTIFACT_PATH_KEYS
            if value.get(key) is not None
        ]
        paths = _dedupe([
            str(item).strip()
            for item in path_values
            if isinstance(item, str) and str(item).strip()
        ])
        if len(paths) != 1:
            _diagnostic(
                diagnostics,
                "errors",
                "ambiguous_route_artifact_path",
                "A route artifact must declare exactly one canonical workspace-relative path.",
                route_id=route_id,
                field=field,
                orchestrator_file=source_file,
            )
            return
        if not _safe_structured_artifact_path(paths[0]):
            _diagnostic(
                diagnostics,
                "errors",
                "unsafe_route_artifact_path",
                "A structured artifact path must be a safe workspace-relative path.",
                route_id=route_id,
                field=field,
                artifact=paths[0],
                orchestrator_file=source_file,
            )
            return
        artifact: dict[str, str] = {"path": paths[0]}
        raw_format = value.get("type") if value.get("type") is not None else value.get("format")
        if raw_format is not None:
            if not isinstance(raw_format, str) or not raw_format.strip():
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_route_artifact_format",
                    "A route artifact format/type must be a non-empty string.",
                    route_id=route_id,
                    field=field,
                    orchestrator_file=source_file,
                )
            else:
                artifact["format"] = raw_format.strip().casefold()
        artifacts.append(artifact)

    for field, declaration in raw_declarations:
        if not isinstance(declaration, dict):
            # Values such as ``output_mode: summary`` and prose deliverable
            # labels do not identify an artifact path and must remain metadata.
            continue
        listed = False
        for list_key in _ROUTE_ARTIFACT_LIST_KEYS:
            if list_key not in declaration:
                continue
            listed = True
            values = declaration.get(list_key)
            if not isinstance(values, list):
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_route_artifact_list",
                    "Route artifact lists must be ordered lists.",
                    route_id=route_id,
                    field=f"{field}.{list_key}",
                    orchestrator_file=source_file,
                )
                continue
            bounded_values = _bounded_sequence(
                values,
                limit=80,
                diagnostics=diagnostics,
                field=f"routes[{route_id}].{field}.{list_key}",
                source_file=source_file,
            )
            for index, value in enumerate(bounded_values):
                add_artifact(value, f"{field}.{list_key}[{index}]")
        if not listed and any(key in declaration for key in _ROUTE_ARTIFACT_PATH_KEYS):
            add_artifact(declaration, field)

    unique: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for artifact in artifacts:
        folded = artifact["path"].casefold()
        if folded in seen_paths:
            _diagnostic(
                diagnostics,
                "errors",
                "duplicate_route_artifact_path",
                "A route may declare each artifact path only once.",
                route_id=route_id,
                artifact=artifact["path"],
                orchestrator_file=source_file,
            )
            continue
        seen_paths.add(folded)
        unique.append(artifact)
    if not unique:
        return {}

    output_contract: dict[str, Any] = {
        "declared_artifacts": [item["path"] for item in unique],
        "declared_file_count": len(unique),
        "route_scoped": True,
    }
    formats = {
        item["path"]: item["format"]
        for item in unique
        if item.get("format")
    }
    if formats:
        output_contract["artifact_formats"] = formats
    return output_contract


def _iter_route_entries(value: Any, prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Return route declarations without recursively following YAML aliases."""
    entries: list[tuple[str, dict[str, Any]]] = []
    stack: list[tuple[Any, str, int]] = [(value, prefix, 0)]
    visited: set[int] = set()
    inspected = 0
    while stack:
        node, node_prefix, depth = stack.pop()
        inspected += 1
        if inspected > MAX_COMPILER_STRUCTURE_NODES or depth > MAX_COMPILER_STRUCTURE_DEPTH:
            break
        if isinstance(node, (dict, list, tuple, set)):
            identity = id(node)
            if identity in visited:
                continue
            visited.add(identity)
        if isinstance(node, list):
            for index, item in enumerate(node):
                if not isinstance(item, dict):
                    continue
                route_id = str(
                    item.get("id")
                    or item.get("name")
                    or f"{node_prefix or 'route'}-{index + 1}"
                )
                entries.append((route_id, item))
            continue
        if not isinstance(node, dict):
            continue
        if _ROUTE_SIGNAL_KEYS.intersection(node):
            route_id = str(node.get("id") or node.get("name") or node_prefix or "route")
            entries.append((route_id, node))
            continue
        children: list[tuple[Any, str, int]] = []
        for key, item in node.items():
            child_prefix = f"{node_prefix}.{key}" if node_prefix else str(key)
            if isinstance(item, str):
                entries.append((child_prefix, {"worker": item}))
            else:
                children.append((item, child_prefix, depth + 1))
        stack.extend(reversed(children))
    return entries


def _normalize_execution_stages(
    route: dict[str, Any],
    *,
    direct_workers: list[str],
    parallel_workers: list[str],
    sequential_workers: list[str],
) -> list[dict[str, Any]]:
    declared = route.get("stages") or route.get("execution_stages") or route.get("waves")
    stages: list[dict[str, Any]] = []
    if isinstance(declared, dict):
        declared = [
            dict(config, id=stage_id)
            if isinstance(config, dict)
            else {"id": stage_id, "worker": config}
            for stage_id, config in declared.items()
        ]
    if isinstance(declared, list):
        previous_stage_terminal: str | None = None
        for index, stage in enumerate(declared):
            if isinstance(stage, str):
                stage_id = f"stage-{index + 1}"
                stages.append(
                    {
                        "id": stage_id,
                        "mode": "sequential",
                        "workers": [stage],
                        "dependencies": (
                            [previous_stage_terminal]
                            if previous_stage_terminal
                            else []
                        ),
                    }
                )
                previous_stage_terminal = stage_id
                continue
            if not isinstance(stage, dict):
                continue
            mode = str(stage.get("mode") or stage.get("spawn_mode") or "sequential").lower()
            if mode in {"serial", "ordered", "chain"}:
                mode = "sequential"
            elif mode in {"concurrent", "fanout", "fan_out", "fan-out"}:
                mode = "parallel"
            elif mode in {"single", "inline"}:
                mode = "direct"
            stage_workers = _as_string_list(
                stage.get("workers") or stage.get("worker") or stage.get("agents")
            )
            stage_id = str(stage.get("id") or stage.get("name") or f"stage-{index + 1}")
            dependencies = _as_string_list(
                stage.get("dependencies") or stage.get("depends_on") or stage.get("after")
            )
            if not dependencies and previous_stage_terminal:
                dependencies = [previous_stage_terminal]
            if mode == "sequential" and len(stage_workers) > 1:
                previous_worker_stage: str | None = None
                for worker_index, worker_id in enumerate(stage_workers):
                    is_terminal = worker_index == len(stage_workers) - 1
                    worker_stage_id = (
                        stage_id
                        if is_terminal
                        else f"{stage_id}-{worker_index + 1}"
                    )
                    worker_dependencies = (
                        list(dependencies)
                        if previous_worker_stage is None
                        else [previous_worker_stage]
                    )
                    stages.append(
                        {
                            "id": worker_stage_id,
                            "mode": "sequential",
                            "workers": [worker_id],
                            "dependencies": worker_dependencies,
                        }
                    )
                    previous_worker_stage = worker_stage_id
                previous_stage_terminal = stage_id
                continue
            stages.append(
                {
                    "id": stage_id,
                    "mode": mode,
                    "workers": stage_workers,
                    "dependencies": dependencies,
                }
            )
            previous_stage_terminal = stage_id
    if stages:
        return stages
    if parallel_workers:
        stages.append(
            {"id": "parallel", "mode": "parallel", "workers": parallel_workers, "dependencies": []}
        )
    if direct_workers:
        stages.append(
            {
                "id": "direct",
                "mode": "direct",
                "workers": direct_workers,
                "dependencies": [stages[-1]["id"]] if stages else [],
            }
        )
    if sequential_workers:
        previous = str(stages[-1]["id"]) if stages else None
        for index, worker_id in enumerate(sequential_workers):
            stage_id = "sequential" if len(sequential_workers) == 1 else f"sequential-{index + 1}"
            dependencies = [previous] if previous else []
            stages.append(
                {
                    "id": stage_id,
                    "mode": "sequential",
                    "workers": [worker_id],
                    "dependencies": dependencies,
                }
            )
            previous = stage_id
    return stages


def _find_dependency_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Find directed dependency cycles and return stable, de-duplicated paths."""
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: dict[tuple[str, ...], list[str]] = {}

    def _canonical_cycle(path: list[str]) -> tuple[str, ...]:
        nodes = path[:-1]
        if not nodes:
            return tuple()
        rotations = [
            tuple(nodes[index:] + nodes[:index])
            for index in range(len(nodes))
        ]
        return min(rotations)

    def _visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for dependency in graph.get(node, []):
            if dependency not in graph:
                continue
            dependency_state = state.get(dependency, 0)
            if dependency_state == 0:
                _visit(dependency)
            elif dependency_state == 1:
                start = stack.index(dependency)
                path = stack[start:] + [dependency]
                key = _canonical_cycle(path)
                if key:
                    cycles.setdefault(key, list(key) + [key[0]])
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            _visit(node)
    return [cycles[key] for key in sorted(cycles)]


def _lint_route_waves(
    *,
    route_id: str,
    waves: list[dict[str, Any]],
    source_file: str,
    diagnostics: dict[str, list[dict[str, Any]]],
) -> None:
    wave_ids: list[str] = [str(wave.get("id") or "").strip() for wave in waves]
    seen_wave_ids: set[str] = set()
    for wave_id in wave_ids:
        if not wave_id:
            continue
        if wave_id in seen_wave_ids:
            _diagnostic(
                diagnostics,
                "errors",
                "duplicate_stage_id",
                f"Route {route_id!r} declares stage/wave id {wave_id!r} more than once.",
                route_id=route_id,
                stage_id=wave_id,
                orchestrator_file=source_file,
            )
        seen_wave_ids.add(wave_id)

    known_wave_ids = {wave_id for wave_id in wave_ids if wave_id}
    graph: dict[str, list[str]] = {}
    for wave in waves:
        wave_id = str(wave.get("id") or "").strip()
        if not wave_id:
            continue
        dependencies = _as_string_list(wave.get("dependencies"))
        graph.setdefault(wave_id, [])
        for dependency in dependencies:
            if dependency not in known_wave_ids:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "missing_stage_dependency",
                    f"Route {route_id!r} stage {wave_id!r} depends on undeclared stage {dependency!r}.",
                    route_id=route_id,
                    stage_id=wave_id,
                    dependency=dependency,
                    orchestrator_file=source_file,
                )
                continue
            graph[wave_id].append(dependency)
    for cycle in _find_dependency_cycles(graph):
        _diagnostic(
            diagnostics,
            "errors",
            "stage_dependency_cycle",
            f"Route {route_id!r} contains a cyclic stage dependency.",
            route_id=route_id,
            cycle=cycle,
            orchestrator_file=source_file,
        )


def _normalize_routes(
    skill_dir: Path,
    orchestrators: list[tuple[str, dict[str, Any]]],
    worker_ids: set[str],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    seen_route_ids: set[str] = set()
    declared_pattern_total = 0
    retained_pattern_total = 0
    global_declaration_order = 0
    reported_total_pattern_limit = False
    reported_route_limit = False
    declared_route_total = 0
    for _, orchestrator in orchestrators:
        route_declaration = orchestrator.get("routing_rules")
        if route_declaration is None:
            route_declaration = orchestrator.get("routes")
        declared_route_total += len(_iter_route_entries(route_declaration))
    if declared_route_total > MAX_SKILL_ROUTES:
        reported_route_limit = True
        _compiler_limit_error(
            diagnostics,
            code="too_many_routes",
            message="Skill declares more than the bounded route limit.",
            field="routes",
            limit=MAX_SKILL_ROUTES,
            actual=declared_route_total,
        )
    for source_file, orchestrator in orchestrators:
        route_declaration = orchestrator.get("routing_rules")
        if route_declaration is None:
            route_declaration = orchestrator.get("routes")
        for route_id, route in _iter_route_entries(route_declaration):
            if global_declaration_order >= MAX_SKILL_ROUTES:
                if not reported_route_limit:
                    reported_route_limit = True
                    _diagnostic(
                        diagnostics,
                        "errors",
                        "too_many_routes",
                        "Skill declares more than the bounded route limit.",
                        field="routes",
                        limit=MAX_SKILL_ROUTES,
                        actual=declared_route_total,
                        orchestrator_file=source_file,
                    )
                break
            declaration_order = global_declaration_order
            global_declaration_order += 1
            if route_id in seen_route_ids:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "duplicate_route_id",
                    f"Route id {route_id!r} is declared more than once.",
                    route_id=route_id,
                    orchestrator_file=source_file,
                )
            seen_route_ids.add(route_id)

            pattern_value = (
                route.get("patterns")
                or route.get("pattern")
                or route.get("regex")
                or route.get("match")
                or route.get("when")
            )
            if isinstance(pattern_value, str):
                declared_pattern_count = 1 if pattern_value.strip() else 0
                declared_patterns = [pattern_value.strip()] if pattern_value.strip() else []
            elif isinstance(pattern_value, dict):
                declared_pattern_count = len(pattern_value)
                declared_patterns = []
                for index, (key, enabled) in enumerate(pattern_value.items()):
                    if index >= MAX_ROUTE_PATTERNS_PER_ROUTE:
                        break
                    if enabled is not False and enabled is not None and str(key).strip():
                        declared_patterns.append(str(key).strip())
            elif isinstance(pattern_value, (list, tuple)):
                declared_pattern_count = len(pattern_value)
                declared_patterns = [
                    str(item).strip()
                    for item in pattern_value[:MAX_ROUTE_PATTERNS_PER_ROUTE]
                    if item is not None and str(item).strip()
                ]
            elif isinstance(pattern_value, set):
                declared_pattern_count = len(pattern_value)
                declared_patterns = []
                for index, item in enumerate(pattern_value):
                    if index >= MAX_ROUTE_PATTERNS_PER_ROUTE:
                        break
                    if item is not None and str(item).strip():
                        declared_patterns.append(str(item).strip())
            else:
                declared_pattern_count = 1 if pattern_value is not None else 0
                declared_patterns = (
                    [str(pattern_value).strip()]
                    if pattern_value is not None and str(pattern_value).strip()
                    else []
                )
            declared_pattern_total += declared_pattern_count
            if declared_pattern_count > MAX_ROUTE_PATTERNS_PER_ROUTE:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "too_many_route_patterns",
                    f"Route {route_id!r} declares more than the bounded pattern limit.",
                    route_id=route_id,
                    declared_count=declared_pattern_count,
                    maximum=MAX_ROUTE_PATTERNS_PER_ROUTE,
                    orchestrator_file=source_file,
                )
            if (
                declared_pattern_total > MAX_ROUTE_PATTERNS_TOTAL
                and not reported_total_pattern_limit
            ):
                reported_total_pattern_limit = True
                _diagnostic(
                    diagnostics,
                    "errors",
                    "too_many_route_patterns_total",
                    "Skill route declarations exceed the package-wide pattern limit.",
                    declared_count=declared_pattern_total,
                    maximum=MAX_ROUTE_PATTERNS_TOTAL,
                    orchestrator_file=source_file,
                )
            retain_count = min(
                MAX_ROUTE_PATTERNS_PER_ROUTE,
                max(0, MAX_ROUTE_PATTERNS_TOTAL - retained_pattern_total),
            )
            patterns = declared_patterns[:retain_count]
            retained_pattern_total += len(patterns)
            invalid_patterns: list[str] = []
            for pattern in patterns:
                validation_error = route_pattern_validation_error(pattern)
                if validation_error is None:
                    continue
                invalid_patterns.append(pattern[:MAX_ROUTE_PATTERN_CHARS])
                error_code = (
                    "invalid_route_pattern"
                    if validation_error.startswith("invalid regular expression:")
                    else "unsafe_route_pattern"
                )
                if len(pattern) > MAX_ROUTE_PATTERN_CHARS:
                    error_code = "route_pattern_too_long"
                _diagnostic(
                    diagnostics,
                    "errors",
                    error_code,
                    f"Route {route_id!r} contains a rejected regular expression: "
                    f"{validation_error}",
                    route_id=route_id,
                    pattern=pattern[:MAX_ROUTE_PATTERN_CHARS],
                    pattern_length=len(pattern),
                    orchestrator_file=source_file,
                )

            spawn_mode = str(route.get("spawn_mode") or route.get("mode") or "").strip().lower()
            single_workers = _as_string_list(route.get("worker"))
            declared_workers = _as_string_list(route.get("workers"))
            parallel_workers = _as_string_list(route.get("parallel_workers"))
            sequential_workers = _as_string_list(route.get("sequential_workers"))
            direct_workers = _as_string_list(route.get("direct_workers"))
            if spawn_mode in {"concurrent", "fanout", "fan_out", "fan-out"}:
                spawn_mode = "parallel"
            elif spawn_mode in {"serial", "ordered", "chain"}:
                spawn_mode = "sequential"
            elif spawn_mode in {"single", "inline"}:
                spawn_mode = "direct"
            if not spawn_mode:
                if single_workers or direct_workers:
                    spawn_mode = "direct"
                elif parallel_workers:
                    spawn_mode = "parallel"
                elif sequential_workers:
                    spawn_mode = "sequential"
                else:
                    spawn_mode = "parallel" if declared_workers else "sequential"
            if declared_workers:
                if spawn_mode == "parallel":
                    parallel_workers = _dedupe(parallel_workers + declared_workers)
                elif spawn_mode == "direct":
                    direct_workers = _dedupe(direct_workers + declared_workers)
                else:
                    sequential_workers = _dedupe(sequential_workers + declared_workers)
            if single_workers:
                if spawn_mode == "parallel":
                    parallel_workers = _dedupe(parallel_workers + single_workers)
                elif spawn_mode == "sequential":
                    sequential_workers = _dedupe(sequential_workers + single_workers)
                else:
                    direct_workers = _dedupe(direct_workers + single_workers)

            waves = _normalize_execution_stages(
                route,
                direct_workers=direct_workers,
                parallel_workers=parallel_workers,
                sequential_workers=sequential_workers,
            )
            _lint_route_waves(
                route_id=route_id,
                waves=waves,
                source_file=source_file,
                diagnostics=diagnostics,
            )
            wave_workers = [
                worker_id
                for wave in waves
                for worker_id in _as_string_list(wave.get("workers"))
            ]
            referenced_workers = _dedupe(
                direct_workers + parallel_workers + sequential_workers + wave_workers
            )
            if not referenced_workers:
                _diagnostic(
                    diagnostics,
                    "warnings",
                    "route_without_workers",
                    f"Route {route_id!r} does not dispatch any declared worker.",
                    route_id=route_id,
                    orchestrator_file=source_file,
                )
            for worker_id in referenced_workers:
                if worker_id not in worker_ids:
                    _diagnostic(
                        diagnostics,
                        "errors",
                        "missing_worker_reference",
                        f"Route {route_id!r} references undeclared worker {worker_id!r}.",
                        route_id=route_id,
                        worker_id=worker_id,
                        orchestrator_file=source_file,
                    )

            priority = _parse_int_literal(route.get("priority"))
            route_order_value = next(
                (
                    route.get(key)
                    for key in ("order", "route_order", "tie_break_order")
                    if route.get(key) is not None
                ),
                None,
            )
            route_order = _parse_int_literal(route_order_value)
            if route_order_value is not None and route_order is None:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_route_order",
                    f"Route {route_id!r} declares a non-numeric route order.",
                    route_id=route_id,
                    value=str(route_order_value)[:200],
                    orchestrator_file=source_file,
                )
            normalized = {
                "id": route_id,
                "description": str(route.get("description") or "").strip()[:800],
                "patterns": [
                    pattern[:MAX_ROUTE_PATTERN_CHARS] for pattern in patterns
                ],
                "invalid_patterns": invalid_patterns,
                "priority": priority if priority is not None else 0,
                "order": route_order,
                "spawn_mode": spawn_mode,
                "direct_workers": direct_workers,
                "parallel_workers": parallel_workers,
                "sequential_workers": sequential_workers,
                "workers": referenced_workers,
                "waves": waves,
                "declaration_order": declaration_order,
                "source_file": source_file,
            }
            for key in ("default", "requires_full_output"):
                value = route.get(key)
                if isinstance(value, bool):
                    normalized[key] = value
                elif value is not None:
                    _diagnostic(
                        diagnostics,
                        "warnings",
                        "invalid_route_boolean",
                        f"Route {route_id!r} field {key!r} must be a boolean.",
                        route_id=route_id,
                        field=key,
                        value=str(value)[:200],
                        orchestrator_file=source_file,
                    )
            for key in ("output_mode", "deliverable", "output_profile"):
                if route.get(key) is not None:
                    normalized[key] = _compact_mapping(
                        route.get(key),
                        max_items=40,
                        max_text=2_000,
                        diagnostics=diagnostics,
                        field=f"routes[{route_id}].{key}",
                        source_file=source_file,
                    )
            if route.get("output") is not None:
                normalized["output"] = _compact_mapping(
                    route.get("output"),
                    max_items=40,
                    max_text=2_000,
                    diagnostics=diagnostics,
                    field=f"routes[{route_id}].output",
                    source_file=source_file,
                )
            route_output_contract = _normalize_route_output_contract(
                route,
                route_id=route_id,
                source_file=source_file,
                diagnostics=diagnostics,
            )
            if route_output_contract:
                normalized["output_contract"] = route_output_contract
            for key in ("required_files", "format_files", "supporting_files"):
                declared_paths = _as_string_list(route.get(key))
                if declared_paths:
                    normalized[key] = declared_paths
                for declared_path in declared_paths:
                    if ".." in Path(declared_path).parts or Path(declared_path).is_absolute():
                        _diagnostic(
                            diagnostics,
                            "errors",
                            "unsafe_route_resource_reference",
                            f"Route {route_id!r} resource path escapes the Skill package.",
                            route_id=route_id,
                            field=key,
                            resource=declared_path,
                            orchestrator_file=source_file,
                        )
                        continue
                    if any(token in declared_path for token in ("{", "}", "$", "<", ">")):
                        continue
                    if any(token in declared_path for token in ("*", "?", "[")):
                        if not any(
                            path.relative_to(skill_dir).match(declared_path)
                            for path in iter_safe_regular_files(
                                skill_dir,
                                skill_dir,
                                excluded_dirs={"__pycache__", "node_modules", ".git"},
                            )
                        ):
                            _diagnostic(
                                diagnostics,
                                "errors",
                                "missing_route_resource_reference",
                                f"Route {route_id!r} resource pattern matches no package file.",
                                route_id=route_id,
                                field=key,
                                resource=declared_path,
                                orchestrator_file=source_file,
                            )
                        continue
                    target = _resolve_local_resource_reference(skill_dir, declared_path)
                    if target is not None and not target.is_file():
                        _diagnostic(
                            diagnostics,
                            "errors",
                            "missing_route_resource_reference",
                            f"Route {route_id!r} references a local resource that does not exist.",
                            route_id=route_id,
                            field=key,
                            resource=declared_path,
                            orchestrator_file=source_file,
                        )
            routes.append({key: value for key, value in normalized.items() if value not in ("", None)})
    return routes


def _normalize_route_selection_policy(
    orchestrators: list[tuple[str, dict[str, Any]]],
    route_ids: set[str],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Preserve only explicit, deterministic route-overlap policies.

    Priority remains a per-route field.  This policy is consulted only when
    multiple matched/default routes share the highest priority and route-level
    ``order`` values do not resolve the tie.
    """
    declarations: list[tuple[str, str, tuple[str, ...]]] = []
    aliases = {
        "declaration_order": "declaration_order",
        "first_declared": "declaration_order",
        "first_match": "declaration_order",
        "first": "declaration_order",
        "reverse_declaration_order": "reverse_declaration_order",
        "last_declared": "reverse_declaration_order",
        "last_match": "reverse_declaration_order",
        "last": "reverse_declaration_order",
        "explicit_order": "explicit_order",
        "route_order": "explicit_order",
        "ordered_routes": "explicit_order",
        "error": "ambiguity",
        "ambiguous": "ambiguity",
        "require_unique": "ambiguity",
    }
    for source_file, orchestrator in orchestrators:
        raw_policy = next(
            (
                orchestrator.get(key)
                for key in (
                    "route_selection_policy",
                    "route_order_policy",
                    "routing_policy",
                )
                if orchestrator.get(key) is not None
            ),
            None,
        )
        raw_order = orchestrator.get("route_order")
        tie_break_value: Any = None
        if isinstance(raw_policy, str):
            tie_break_value = raw_policy
        elif isinstance(raw_policy, dict):
            tie_break_value = next(
                (
                    raw_policy.get(key)
                    for key in ("tie_break", "tie_breaker", "on_tie", "on_overlap")
                    if raw_policy.get(key) is not None
                ),
                None,
            )
            if raw_order is None:
                raw_order = next(
                    (
                        raw_policy.get(key)
                        for key in ("route_order", "ordered_routes", "order")
                        if raw_policy.get(key) is not None
                    ),
                    None,
                )
        elif raw_policy is not None:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_route_selection_policy",
                "Route selection policy must be a string or mapping.",
                orchestrator_file=source_file,
            )
            continue

        order = tuple(_as_string_list(raw_order))
        if tie_break_value is None and order:
            tie_break_value = "explicit_order"
        if tie_break_value is None:
            continue
        normalized_value = re.sub(
            r"[\s-]+", "_", str(tie_break_value).strip().casefold()
        )
        tie_break = aliases.get(normalized_value)
        if tie_break is None:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_route_selection_policy",
                "Route selection policy declares an unsupported tie-break rule.",
                value=str(tie_break_value)[:200],
                orchestrator_file=source_file,
            )
            continue
        if tie_break == "explicit_order" and not order:
            _diagnostic(
                diagnostics,
                "errors",
                "missing_route_order",
                "Explicit-order route selection policy declares no route order.",
                orchestrator_file=source_file,
            )
            continue
        if len(set(order)) != len(order):
            _diagnostic(
                diagnostics,
                "errors",
                "duplicate_route_order_id",
                "Explicit route order repeats a route id.",
                route_order=list(order),
                orchestrator_file=source_file,
            )
        unknown = [route_id for route_id in order if route_id not in route_ids]
        if unknown:
            _diagnostic(
                diagnostics,
                "errors",
                "unknown_route_order_id",
                "Explicit route order references undeclared routes.",
                route_ids=unknown,
                orchestrator_file=source_file,
            )
        declarations.append((source_file, tie_break, order))

    unique = {(tie_break, order) for _, tie_break, order in declarations}
    if len(unique) > 1:
        _diagnostic(
            diagnostics,
            "errors",
            "conflicting_route_selection_policy",
            "Orchestrator files declare conflicting route selection policies.",
            declarations=[
                {
                    "source_file": source_file,
                    "tie_break": tie_break,
                    "route_order": list(order),
                }
                for source_file, tie_break, order in declarations
            ],
        )
        return {}
    if not declarations:
        return {}
    _, tie_break, order = declarations[0]
    result: dict[str, Any] = {"tie_break": tie_break}
    if order:
        result["route_order"] = list(order)
    return result


def _bootstrap_retrieval_completeness_policy(
    item: dict[str, Any],
    *,
    diagnostics: dict[str, list[dict[str, Any]]],
    source_file: str,
    index: int,
) -> str | None:
    """Compile finite HTTP pagination semantics from explicit source metadata."""

    declarations: list[tuple[str, str]] = []
    for key in (
        "retrieval_completeness_policy",
        "retrieval_mode",
        "pagination_mode",
    ):
        if item.get(key) is None:
            continue
        try:
            declarations.append((
                key,
                normalize_retrieval_completeness_policy(item.get(key)),
            ))
        except ValueError as exc:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_retrieval_completeness_policy",
                str(exc),
                field=f"knowledge_bootstrap.sources[{index}].{key}",
                orchestrator_file=source_file,
            )
            return None
    for key in ("exhaustive", "complete_all_pages", "all_pages"):
        if key not in item:
            continue
        value = item.get(key)
        if not isinstance(value, bool):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_retrieval_completeness_policy",
                f"{key} must be boolean when declared.",
                field=f"knowledge_bootstrap.sources[{index}].{key}",
                orchestrator_file=source_file,
            )
            return None
        declarations.append((
            key,
            (
                RETRIEVAL_COMPLETENESS_POLICY_EXHAUSTIVE
                if value else RETRIEVAL_COMPLETENESS_POLICY_BOUNDED
            ),
        ))
    policies = {policy for _key, policy in declarations}
    if len(policies) > 1:
        _diagnostic(
            diagnostics,
            "errors",
            "conflicting_retrieval_completeness_policy",
            "Bootstrap source declares conflicting bounded/exhaustive retrieval policies.",
            field=f"knowledge_bootstrap.sources[{index}]",
            declarations=[
                {"field": key, "policy": policy}
                for key, policy in declarations
            ],
            orchestrator_file=source_file,
        )
        return None
    return next(iter(policies), None)


def _normalize_bootstrap(
    skill_dir: Path,
    orchestrators: list[tuple[str, dict[str, Any]]],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    descriptions: list[str] = []
    shared_context_templates: list[str] = []
    for source_file, orchestrator in orchestrators:
        bootstrap = orchestrator.get("knowledge_bootstrap")
        if isinstance(bootstrap, list):
            bootstrap = {"sources": bootstrap}
        if not isinstance(bootstrap, dict):
            continue
        description = bootstrap.get("description")
        if description:
            descriptions.append(
                _bounded_text(
                    description,
                    limit=1_000,
                    diagnostics=diagnostics,
                    field="knowledge_bootstrap.description",
                    source_file=source_file,
                )
            )
        template = bootstrap.get("shared_context_template")
        if template:
            shared_context_templates.append(
                _bounded_text(
                    template,
                    limit=4_000,
                    diagnostics=diagnostics,
                    field="knowledge_bootstrap.shared_context_template",
                    source_file=source_file,
                )
            )
        declared_sources = (
            bootstrap.get("pre_fetch_sources")
            or bootstrap.get("sources")
            or bootstrap.get("providers")
        )
        if isinstance(declared_sources, dict):
            declared_sources = [
                dict(value, name=key) if isinstance(value, dict) else {"name": key, "source": value}
                for key, value in declared_sources.items()
            ]
        if not isinstance(declared_sources, list):
            continue
        declared_sources = _bounded_sequence(
            declared_sources,
            limit=80,
            diagnostics=diagnostics,
            field="knowledge_bootstrap.sources",
            source_file=source_file,
        )
        for index, item in enumerate(declared_sources):
            if isinstance(item, str):
                normalized = {"id": item, "name": item}
            elif isinstance(item, dict):
                normalized = {
                    key: _compact_mapping(
                        item.get(key),
                        max_items=30,
                        diagnostics=diagnostics,
                        field=f"knowledge_bootstrap.sources[{index}].{key}",
                        source_file=source_file,
                    )
                    for key in (
                        "id",
                        "name",
                        "skill",
                        "tool",
                        "capability",
                        "tools",
                        "capabilities",
                        "skills",
                        "source",
                        "path",
                        "paths",
                        "file",
                        "files",
                        "resource",
                        "resources",
                        "query_strategy",
                        "extract_fields",
                        "applicable_guidelines",
                        "required",
                    )
                    if item.get(key) is not None
                }
                normalized.setdefault(
                    "id",
                    str(item.get("name") or item.get("skill") or item.get("tool") or f"source-{index + 1}"),
                )
            else:
                continue
            if isinstance(item, dict):
                retrieval_policy = _bootstrap_retrieval_completeness_policy(
                    item,
                    diagnostics=diagnostics,
                    source_file=source_file,
                    index=index,
                )
                if retrieval_policy is not None:
                    normalized["retrieval_completeness_policy"] = (
                        retrieval_policy
                    )
                local_resources = _extract_declared_local_resources(
                    item,
                    skill_dir,
                    diagnostics,
                    field=f"knowledge_bootstrap.sources[{index}]",
                    source_file=source_file,
                )
                if local_resources:
                    normalized["local_resources"] = local_resources
                environment_contract = _compile_nested_environment_contract(
                    item,
                    source_file=source_file,
                    diagnostics=diagnostics,
                    include_dependencies=False,
                )
                if environment_contract:
                    normalized["environment_contract"] = environment_contract
            normalized["source_file"] = source_file
            sources.append(normalized)
    result = {
        "descriptions": _dedupe(descriptions),
        "sources": sources,
        "shared_context_templates": _dedupe(shared_context_templates),
    }
    return {key: value for key, value in result.items() if value}


_INTENT_WORKER_MAP_KEYS = {
    "worker_map",
    "workers_map",
    "worker_mapping",
    "workers_mapping",
}

_INTENT_LOCAL_RESOURCE_MAP_KEYS = {
    "knowledge_source_map",
    "knowledge_sources_map",
    "resource_map",
    "resources_map",
    "source_map",
    "sources_map",
    "skill_map",
    "phase_skill_map",
    "reference_map",
    "references_map",
    "file_map",
    "files_map",
}


def _iter_mapping_leaf_strings(
    value: Any,
    *,
    diagnostics: dict[str, list[dict[str, Any]]] | None = None,
    field: str = "intent_mapping",
    source_file: str | None = None,
) -> list[str]:
    """Collect scalar leaves iteratively with identity and size bounds."""
    strings: list[str] = []
    stack: list[tuple[Any, int, str]] = [(value, 0, field)]
    visited: set[int] = set()
    node_count = 0
    scalar_chars = 0
    while stack:
        node, depth, path = stack.pop()
        node_count += 1
        if node_count > MAX_COMPACT_STRUCTURE_NODES:
            _compiler_limit_error(
                diagnostics,
                code="compiler_structure_node_limit_exceeded",
                message="An intent mapping exceeds the compiler node limit.",
                field=field,
                limit=MAX_COMPACT_STRUCTURE_NODES,
                actual=node_count,
                source_file=source_file,
                yaml_path=path,
            )
            break
        if depth > MAX_COMPACT_STRUCTURE_DEPTH:
            _compiler_limit_error(
                diagnostics,
                code="compiler_structure_depth_limit_exceeded",
                message="An intent mapping exceeds the compiler nesting limit.",
                field=field,
                limit=MAX_COMPACT_STRUCTURE_DEPTH,
                actual=depth,
                source_file=source_file,
                yaml_path=path,
            )
            continue
        if isinstance(node, (dict, list, tuple, set)):
            identity = id(node)
            if identity in visited:
                continue
            visited.add(identity)
            if isinstance(node, dict):
                children = list(node.values())
            else:
                children = list(node)
            stack.extend(
                (item, depth + 1, f"{path}[{index}]")
                for index, item in reversed(list(enumerate(children)))
            )
            continue
        if not isinstance(node, str) or not node.strip():
            continue
        scalar_chars += len(node)
        if scalar_chars > MAX_COMPACT_SCALAR_CHARS:
            _compiler_limit_error(
                diagnostics,
                code="compiler_scalar_chars_limit_exceeded",
                message="An intent mapping exceeds the aggregate scalar-text limit.",
                field=field,
                limit=MAX_COMPACT_SCALAR_CHARS,
                actual=scalar_chars,
                source_file=source_file,
                yaml_path=path,
            )
            break
        strings.append(node.strip())
    return strings


def _looks_like_external_resource_reference(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    if re.match(r"^[a-z][a-z0-9+.-]*://", lowered):
        return True
    if re.match(r"^(?:skill|tool|mcp|app|api|websearch|web_search|web-fetch|web_fetch):", lowered):
        return True
    if lowered in {
        "websearch",
        "web_search",
        "webfetch",
        "web_fetch",
        "internet",
        "external_search",
    }:
        return True
    if any(character in value for character in ("{", "}", "$")):
        return True
    return False


def _resolve_local_resource_reference(skill_dir: Path, value: str) -> Path | None:
    reference = value.strip().replace("\\", "/")
    if not reference or _looks_like_external_resource_reference(reference):
        return None
    suffix = Path(reference).suffix.lower()
    if "/" not in reference and suffix not in _TEXT_RESOURCE_SUFFIXES:
        return None

    direct_check = validate_skill_resource(
        skill_dir,
        reference,
        expected_kind="file",
        require_relative=True,
    )
    if direct_check.valid and direct_check.path is not None:
        return direct_check.path
    if "/" not in reference:
        matches = [
            path
            for path in iter_safe_regular_files(
                skill_dir,
                skill_dir,
                excluded_dirs={"__pycache__", "node_modules", ".git"},
            )
            if path.name == reference
            and not any(part in {"__pycache__", "node_modules", ".git"} for part in path.parts)
        ]
        if len(matches) == 1:
            return matches[0]
    reason = direct_check.code or "missing_resource"
    return skill_dir / f"__invalid_local_resource_{reason}__"


def _normalize_intent_map(
    value: Any,
    *,
    diagnostics: dict[str, list[dict[str, Any]]],
    field: str,
    source_file: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _compact_mapping(
            item,
            max_items=80,
            max_text=1_500,
            diagnostics=diagnostics,
            field=f"{field}.{key}",
            source_file=source_file,
        )
        for key, item in _bounded_sequence(
            list(value.items()),
            limit=80,
            diagnostics=diagnostics,
            field=field,
            source_file=source_file,
        )
    }


def _normalize_intent_classification(
    skill_dir: Path,
    orchestrators: list[tuple[str, dict[str, Any]]],
    worker_ids: set[str],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    descriptions: list[str] = []
    dimensions: list[dict[str, Any]] = []
    seen_dimensions: dict[str, str] = {}

    for source_file, orchestrator in orchestrators:
        classification = orchestrator.get("intent_classification")
        if not isinstance(classification, dict):
            continue
        description = classification.get("description")
        if description:
            descriptions.append(
                _bounded_text(
                    description,
                    limit=2_000,
                    diagnostics=diagnostics,
                    field="intent_classification.description",
                    source_file=source_file,
                )
            )
        declared_dimensions = classification.get("dimensions")
        if isinstance(declared_dimensions, list):
            dimension_entries = []
            for index, config in enumerate(declared_dimensions):
                if not isinstance(config, dict):
                    continue
                dimension_id = (
                    config.get("id")
                    or config.get("name")
                    or f"dimension-{index + 1}"
                )
                dimension_entries.append((str(dimension_id), config))
        elif isinstance(declared_dimensions, dict):
            dimension_entries = [
                (
                    str(dimension_id),
                    config if isinstance(config, dict) else {"values": config},
                )
                for dimension_id, config in declared_dimensions.items()
            ]
        else:
            continue

        dimension_entries = [
            entry
            for entry in _bounded_sequence(
                dimension_entries,
                limit=80,
                diagnostics=diagnostics,
                field="intent_classification.dimensions",
                source_file=source_file,
            )
            if isinstance(entry, tuple) and len(entry) == 2
        ]
        for dimension_id, config in dimension_entries:
            if dimension_id in seen_dimensions:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "duplicate_intent_dimension_id",
                    f"Intent dimension {dimension_id!r} is declared more than once.",
                    dimension_id=dimension_id,
                    source_file=source_file,
                    previous_source_file=seen_dimensions[dimension_id],
                )
            else:
                seen_dimensions[dimension_id] = source_file

            raw_values_value = (
                config.get("values")
                or config.get("allowed_values")
                or config.get("options")
                or config.get("enum")
            )
            if isinstance(raw_values_value, (list, tuple, set)):
                raw_values = [
                    str(value).strip()
                    for value in raw_values_value
                    if value is not None and str(value).strip()
                ]
            else:
                raw_values = _as_string_list(raw_values_value)
            values = _dedupe(raw_values)
            values = [
                str(value)
                for value in _bounded_sequence(
                    values,
                    limit=80,
                    diagnostics=diagnostics,
                    field=f"intent_classification.{dimension_id}.values",
                    source_file=source_file,
                )
            ]
            value_counts: dict[str, int] = {}
            for value in raw_values:
                value_counts[value] = value_counts.get(value, 0) + 1
            duplicate_values = sorted(
                value for value, count in value_counts.items() if count > 1
            )
            if duplicate_values:
                _diagnostic(
                    diagnostics,
                    "warnings",
                    "duplicate_intent_dimension_values",
                    f"Intent dimension {dimension_id!r} repeats allowed values.",
                    dimension_id=dimension_id,
                    values=duplicate_values,
                    source_file=source_file,
                )

            mappings: dict[str, Any] = {}
            reserved_keys = {
                "id",
                "name",
                "description",
                "values",
                "allowed_values",
                "options",
                "enum",
                "default",
                "required",
                "on_missing",
                "nullable",
            }
            for map_name, map_value in config.items():
                if map_name in reserved_keys or not map_name.endswith(("_map", "_mapping", "_rules")):
                    continue
                normalized_map = _normalize_intent_map(
                    map_value,
                    diagnostics=diagnostics,
                    field=f"intent_classification.{dimension_id}.{map_name}",
                    source_file=source_file,
                )
                if normalized_map:
                    mappings[str(map_name)] = normalized_map
                if normalized_map and values:
                    unknown_keys = [
                        str(key)
                        for key in normalized_map
                        if str(key) not in values
                    ]
                    if unknown_keys:
                        _diagnostic(
                            diagnostics,
                            "warnings",
                            "intent_mapping_unknown_value",
                            f"Intent mapping {map_name!r} contains keys absent from the dimension values.",
                            dimension_id=dimension_id,
                            mapping=map_name,
                            values=unknown_keys,
                            source_file=source_file,
                        )

                if map_name in _INTENT_WORKER_MAP_KEYS and normalized_map:
                    for intent_value, declared_workers in normalized_map.items():
                        for worker_id in _as_string_list(declared_workers):
                            if worker_id not in worker_ids:
                                _diagnostic(
                                    diagnostics,
                                    "errors",
                                    "missing_intent_worker_reference",
                                    f"Intent dimension {dimension_id!r} references undeclared worker {worker_id!r}.",
                                    dimension_id=dimension_id,
                                    mapping=map_name,
                                    intent_value=str(intent_value),
                                    worker_id=worker_id,
                                    source_file=source_file,
                                )

                if map_name in _INTENT_LOCAL_RESOURCE_MAP_KEYS:
                    for reference in _iter_mapping_leaf_strings(
                        normalized_map,
                        diagnostics=diagnostics,
                        field=f"intent_classification.{dimension_id}.{map_name}",
                        source_file=source_file,
                    ):
                        target = _resolve_local_resource_reference(skill_dir, reference)
                        if target is None:
                            continue
                        if not target.is_file():
                            _diagnostic(
                                diagnostics,
                                "errors",
                                "missing_intent_resource_reference",
                                f"Intent dimension {dimension_id!r} references a local resource that does not exist.",
                                dimension_id=dimension_id,
                                mapping=map_name,
                                resource=reference,
                                source_file=source_file,
                            )

            normalized_dimension = {
                "id": dimension_id,
                "description": _bounded_text(
                    config.get("description"),
                    limit=1_000,
                    diagnostics=diagnostics,
                    field=f"intent_classification.{dimension_id}.description",
                    source_file=source_file,
                ),
                "values": values,
                "default": _compact_mapping(
                    config.get("default"),
                    diagnostics=diagnostics,
                    field=f"intent_classification.{dimension_id}.default",
                    source_file=source_file,
                ),
                "required": config.get("required"),
                "on_missing": str(config.get("on_missing") or "").strip().lower(),
                "nullable": config.get("nullable") if isinstance(config.get("nullable"), bool) else None,
                "mappings": mappings,
                "source_file": source_file,
            }
            if config.get("required") is True and not values:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "required_intent_dimension_without_values",
                    f"Required intent dimension {dimension_id!r} declares no allowed values.",
                    dimension_id=dimension_id,
                    source_file=source_file,
                )
            default_value = config.get("default")
            if (
                default_value is not None
                and values
                and str(default_value) not in values
            ):
                _diagnostic(
                    diagnostics,
                    "errors",
                    "intent_default_unknown_value",
                    f"Intent dimension {dimension_id!r} has a default outside its allowed values.",
                    dimension_id=dimension_id,
                    default=str(default_value),
                    source_file=source_file,
                )
            # Also expose named mappings directly for straightforward consumers,
            # while retaining the generic mappings container for unknown schemas.
            for map_name, map_value in mappings.items():
                normalized_dimension[map_name] = map_value
            dimensions.append(
                {
                    key: value
                    for key, value in normalized_dimension.items()
                    if value not in ("", None, [], {})
                }
            )

    result = {
        "descriptions": _dedupe(descriptions),
        "dimensions": dimensions,
    }
    return {key: value for key, value in result.items() if value}


def _normalize_check(
    check: Any,
    index: int,
    *,
    diagnostics: dict[str, list[dict[str, Any]]],
    field: str,
    source_file: str,
) -> dict[str, Any]:
    if isinstance(check, str):
        return {"id": f"check-{index + 1}", "description": check}
    if not isinstance(check, dict):
        return {"id": f"check-{index + 1}", "description": str(check)}
    check_id = check.get("id") or check.get("name") or check.get("check") or f"check-{index + 1}"
    normalized = {
        "id": str(check_id),
        "description": str(check.get("description") or check.get("rule") or check.get("check") or "").strip(),
    }
    for key in ("severity", "expression", "pass_criteria", "action", "required"):
        if check.get(key) is not None:
            normalized[key] = _compact_mapping(
                check.get(key),
                diagnostics=diagnostics,
                field=f"{field}.{key}",
                source_file=source_file,
            )
    return {key: value for key, value in normalized.items() if value not in ("", None)}


def _normalize_aggregation(
    skill_dir: Path,
    orchestrators: list[tuple[str, dict[str, Any]]],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    descriptions: list[str] = []
    steps: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for source_file, orchestrator in orchestrators:
        aggregation = orchestrator.get("aggregation")
        top_level_checks: Any = []
        if isinstance(aggregation, list):
            declared_steps = aggregation
        elif isinstance(aggregation, dict):
            if aggregation.get("description"):
                descriptions.append(
                    _bounded_text(
                        aggregation.get("description"),
                        limit=1_000,
                        diagnostics=diagnostics,
                        field="aggregation.description",
                        source_file=source_file,
                    )
                )
            declared_steps = aggregation.get("steps") or aggregation.get("stages") or []
            top_level_checks = aggregation.get("checks") or aggregation.get("rules") or []
        else:
            continue
        if isinstance(declared_steps, dict):
            declared_steps = [
                dict(config, id=step_id) if isinstance(config, dict) else {"id": step_id, "description": config}
                for step_id, config in declared_steps.items()
            ]
        if not isinstance(declared_steps, list):
            continue
        declared_steps = _bounded_sequence(
            declared_steps,
            limit=80,
            diagnostics=diagnostics,
            field="aggregation.steps",
            source_file=source_file,
        )
        for index, step in enumerate(declared_steps):
            if isinstance(step, str):
                normalized_step = {"id": step, "description": step}
                declared_checks: Any = []
            elif isinstance(step, dict):
                step_id = step.get("step") or step.get("id") or step.get("name") or f"step-{index + 1}"
                normalized_step = {
                    "id": str(step_id),
                    "description": _bounded_text(
                        step.get("description"),
                        limit=1_000,
                        diagnostics=diagnostics,
                        field=f"aggregation.steps[{index}].description",
                        source_file=source_file,
                    ),
                }
                raw_mode = step.get("mode")
                if isinstance(raw_mode, str) and raw_mode.strip():
                    normalized_step["mode"] = raw_mode.strip().casefold()
                for key in ("method", "output", "required"):
                    if step.get(key) is not None:
                        normalized_step[key] = _compact_mapping(
                            step.get(key),
                            max_items=30,
                            diagnostics=diagnostics,
                            field=f"aggregation.steps[{index}].{key}",
                            source_file=source_file,
                        )
                for key in ("tools", "capabilities", "skills"):
                    if step.get(key) is not None:
                        normalized_step[key] = _compact_mapping(
                            step.get(key),
                            max_items=80,
                            max_text=1_000,
                            diagnostics=diagnostics,
                            field=f"aggregation.steps[{index}].{key}",
                            source_file=source_file,
                        )
                local_resources = _extract_declared_local_resources(
                    step,
                    skill_dir,
                    diagnostics,
                    field=f"aggregation.steps[{index}]",
                    source_file=source_file,
                )
                if local_resources:
                    normalized_step["local_resources"] = local_resources
                environment_contract = _compile_nested_environment_contract(
                    step,
                    source_file=source_file,
                    diagnostics=diagnostics,
                    include_dependencies=False,
                )
                if environment_contract:
                    normalized_step["environment_contract"] = (
                        environment_contract
                    )
                if "depends_on" in step:
                    compact_dependencies = _compact_mapping(
                        step.get("depends_on"),
                        max_items=30,
                        diagnostics=diagnostics,
                        field=f"aggregation.steps[{index}].depends_on",
                        source_file=source_file,
                    )
                    normalized_step["depends_on"] = _as_string_list(
                        compact_dependencies
                    )
                    normalized_step["dependencies_declared"] = True
                declared_checks = step.get("checks") or step.get("rules") or []
            else:
                continue
            normalized_checks: list[dict[str, Any]] = []
            if isinstance(declared_checks, dict):
                declared_checks = [
                    dict(value, id=key) if isinstance(value, dict) else {"id": key, "description": value}
                    for key, value in declared_checks.items()
                ]
            if isinstance(declared_checks, list):
                declared_checks = _bounded_sequence(
                    declared_checks,
                    limit=80,
                    diagnostics=diagnostics,
                    field=f"aggregation.steps[{index}].checks",
                    source_file=source_file,
                )
                for check_index, check in enumerate(declared_checks):
                    normalized_check = _normalize_check(
                        check,
                        check_index,
                        diagnostics=diagnostics,
                        field=f"aggregation.steps[{index}].checks[{check_index}]",
                        source_file=source_file,
                    )
                    normalized_checks.append(normalized_check)
                    checks.append(
                        dict(normalized_check, aggregation_step=normalized_step["id"], source_file=source_file)
                    )
            if normalized_checks:
                normalized_step["checks"] = normalized_checks
            normalized_step["source_file"] = source_file
            steps.append(
                {
                    key: value
                    for key, value in normalized_step.items()
                    if value not in ("", None, [], {})
                }
            )
        if isinstance(top_level_checks, dict):
            top_level_checks = [
                dict(value, id=key) if isinstance(value, dict) else {"id": key, "description": value}
                for key, value in top_level_checks.items()
            ]
        if isinstance(top_level_checks, list):
            top_level_checks = _bounded_sequence(
                top_level_checks,
                limit=80,
                diagnostics=diagnostics,
                field="aggregation.checks",
                source_file=source_file,
            )
            for check_index, check in enumerate(top_level_checks):
                normalized_check = _normalize_check(
                    check,
                    check_index,
                    diagnostics=diagnostics,
                    field=f"aggregation.checks[{check_index}]",
                    source_file=source_file,
                )
                checks.append(
                    dict(normalized_check, aggregation_step="aggregation", source_file=source_file)
                )
    infer_ordered_dependencies = not any(
        step.get("dependencies_declared") is True
        for step in steps
    )
    previous_step_id = ""
    for step in steps:
        if (
            infer_ordered_dependencies
            and previous_step_id
            and step.get("dependencies_declared") is not True
            and str(step.get("mode") or "").casefold() != "parallel"
        ):
            # An ordered aggregation ``steps`` list is sequential by default.
            # Skills can opt out with ``mode: parallel`` or an explicit
            # ``depends_on: []``.  This ensures each later synthesis pass reads
            # the persisted result of the pass immediately before it.
            step["depends_on"] = [previous_step_id]
        step.pop("dependencies_declared", None)
        previous_step_id = str(step.get("id") or previous_step_id)
    result = {"descriptions": _dedupe(descriptions), "steps": steps, "checks": checks}
    return {key: value for key, value in result.items() if value}


def _normalize_conflict_resolution(
    orchestrators: list[tuple[str, dict[str, Any]]],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    descriptions: list[str] = []
    strategies: list[dict[str, Any]] = []
    templates: list[str] = []
    for source_file, orchestrator in orchestrators:
        conflict = orchestrator.get("conflict_resolution")
        if not isinstance(conflict, dict):
            continue
        if conflict.get("description"):
            descriptions.append(
                _bounded_text(
                    conflict.get("description"),
                    limit=1_000,
                    diagnostics=diagnostics,
                    field="conflict_resolution.description",
                    source_file=source_file,
                )
            )
        template = conflict.get("contradiction_report_template") or conflict.get("template")
        if template:
            templates.append(
                _bounded_text(
                    template,
                    limit=4_000,
                    diagnostics=diagnostics,
                    field="conflict_resolution.template",
                    source_file=source_file,
                )
            )
        declared = conflict.get("strategies") or conflict.get("rules") or []
        if isinstance(declared, dict):
            declared = [
                dict(value, name=key) if isinstance(value, dict) else {"name": key, "description": value}
                for key, value in declared.items()
            ]
        if not isinstance(declared, list):
            continue
        declared = _bounded_sequence(
            declared,
            limit=80,
            diagnostics=diagnostics,
            field="conflict_resolution.strategies",
            source_file=source_file,
        )
        for index, strategy in enumerate(declared):
            if isinstance(strategy, str):
                normalized = {"name": strategy, "priority": index + 1}
            elif isinstance(strategy, dict):
                normalized = {
                    key: _compact_mapping(
                        strategy.get(key),
                        max_items=30,
                        diagnostics=diagnostics,
                        field=f"conflict_resolution.strategies[{index}].{key}",
                        source_file=source_file,
                    )
                    for key in ("name", "id", "priority", "description", "applies_to", "rationale", "rule")
                    if strategy.get(key) is not None
                }
                normalized.setdefault("name", str(strategy.get("id") or f"strategy-{index + 1}"))
            else:
                continue
            normalized["source_file"] = source_file
            strategies.append(normalized)
    result = {
        "descriptions": _dedupe(descriptions),
        "strategies": strategies,
        "report_templates": _dedupe(templates),
    }
    return {key: value for key, value in result.items() if value}


def _split_numbered_rules(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return []
    rules: list[str] = []
    current = ""
    found_marker = False
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^(?:\d+[.)、]|[-*])\s*", stripped):
            found_marker = True
            if current:
                rules.append(current)
            current = re.sub(r"^(?:\d+[.)、]|[-*])\s*", "", stripped)
        elif found_marker and current:
            current = f"{current} {stripped}"
    if current:
        rules.append(current)
    if not found_marker and value.strip():
        rules.append(" ".join(line.strip() for line in value.splitlines() if line.strip()))
    return rules


def _stable_required_marker(raw_marker: str) -> str:
    """Turn a templated bold field into a stable substring suitable for checks."""
    marker = str(raw_marker).strip()
    if marker.startswith("**") and marker.endswith("**"):
        marker = marker[2:-2].strip()
    placeholder_positions = [
        position
        for token in ("[", "{", "<")
        if (position := marker.find(token)) >= 0
    ]
    if placeholder_positions:
        marker = marker[:min(placeholder_positions)].rstrip()
        marker = marker.rstrip(",，;； ")
        if marker.endswith((":", "：")):
            return f"**{marker}"
    if not marker:
        return ""
    return f"**{marker}**"


def _extract_bold_markers(value: str) -> list[str]:
    markers: list[str] = []
    for raw_marker in re.findall(r"\*\*([^*\n]{1,160})\*\*", value):
        marker = _stable_required_marker(raw_marker)
        if marker:
            markers.append(marker)
    return markers


def _is_module_template_context(text: str, block_start: int) -> bool:
    heading_matches = list(
        re.finditer(
            r"^#{1,6}\s+(.+?)\s*$",
            text[:block_start],
            re.MULTILINE,
        )
    )
    heading = heading_matches[-1].group(1).strip() if heading_matches else ""
    if re.search(
        r"(?:checklist|audit|troubleshoot|incident|failure|debug|"
        r"清单|核验|审计|故障|事故|调试)",
        heading,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"(?:\b(?:module|modular|content|output)?\s*file\s+template\b|"
        r"\bmodule\s+template\b|\bartifact\s+template\b|"
        r"(?:模块|内容|输出)?文件模板|模块模板|交付物模板)",
        heading,
        re.IGNORECASE,
    ):
        return True
    context_start = heading_matches[-1].end() if heading_matches else max(0, block_start - 500)
    context = text[context_start:block_start]
    return bool(
        re.search(
            r"(?:each|every|all)\s+(?:output|content|module|modular)?\s*files?"
            r".{0,120}(?:follow|use|structure|template)|"
            r"(?:每个|所有|各)(?:输出|内容|模块化)?文件.{0,80}(?:结构|模板|格式)",
            context,
            re.IGNORECASE | re.DOTALL,
        )
    )


def _extract_required_module_markers(
    text: str,
    requirements: list[str],
) -> list[str]:
    """Extract structural markers explicitly required for every module file."""
    markers: list[str] = []
    for match in re.finditer(
        r"```(?:markdown|md)?\s*\n(.*?)```",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        if _is_module_template_context(text, match.start()):
            markers.extend(_extract_bold_markers(match.group(1)))

    for requirement in requirements:
        universal_scope = re.search(
            r"(?:each|every|all)\s+(?:output|content|module|modular)?\s*files?|"
            r"(?:每个|所有|各)(?:输出|内容|模块化)?文件",
            requirement,
            re.IGNORECASE,
        )
        if not universal_scope:
            continue
        for literal in re.findall(r"`([^`\n]{1,180})`", requirement):
            markers.extend(_extract_bold_markers(literal))
        if re.search(
            r"(?:prefix(?:ed)?|marker|label(?:ed|led)?|format|"
            r"前缀|标记|标签|格式)",
            requirement,
            re.IGNORECASE,
        ):
            markers.extend(_extract_bold_markers(requirement))
    return _dedupe(markers)


def _parse_format_quality_contract(
    skill_dir: Path,
    declared_format_files: list[str] | None = None,
) -> dict[str, Any]:
    formats_dir = skill_dir / "formats"
    if not validate_skill_resource(
        skill_dir, formats_dir, expected_kind="directory"
    ).valid:
        return {}
    requirements: list[str] = []
    template_markers: list[str] = []
    required_module_markers: list[str] = []
    section_file_mapping: list[dict[str, Any]] = []
    constraints: dict[str, int] = {}
    format_files: list[str] = []
    if declared_format_files is not None:
        candidates = [
            skill_dir / relative_path
            for relative_path in declared_format_files
            if str(relative_path).startswith("formats/")
        ]
    else:
        candidates = [
            path
            for path in iter_safe_regular_files(skill_dir, formats_dir)
            if path.parent == formats_dir and path.suffix.lower() == ".md"
        ]
    for path in candidates:
        if not validate_skill_resource(skill_dir, path, expected_kind="file").valid:
            continue
        text, _, source_truncated, source_unreadable = (
            _read_semantic_text_resource(path, skill_dir)
        )
        # Quality/format requirements are authoritative compiler input.  A
        # presentation prefix is insufficient because tail declarations can
        # change the artifact contract.
        if source_truncated or source_unreadable or not text:
            continue
        relative_path = str(path.relative_to(skill_dir))
        format_files.append(relative_path)
        current_requirements: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            if (
                any(token in lowered for token in ("must", "required", "every file", "each file"))
                or any(token in stripped for token in ("必须", "每个文件", "不得"))
            ) and 0 < len(stripped) <= 500:
                requirement = stripped.strip("-* ")
                requirements.append(requirement)
                current_requirements.append(requirement)
        for block in re.findall(r"```(?:markdown|md)?\s*\n(.*?)```", text, re.IGNORECASE | re.DOTALL):
            for marker in re.findall(r"(\*\*[^*\n]{1,160}\*\*)", block):
                template_markers.append(marker.strip())
        required_module_markers.extend(
            _extract_required_module_markers(text, current_requirements)
        )
        for row in re.finditer(
            r"^\|\s*`?([^|`\n]+\.md)`?\s*\|\s*([^|\n]+)\|",
            text,
            re.MULTILINE | re.IGNORECASE,
        ):
            filename, mapped_sections = row.groups()
            filename = filename.strip()
            if _safe_structured_artifact_path(filename):
                section_file_mapping.append(
                    {
                        "file": filename,
                        "sections": mapped_sections.strip(),
                        "source_file": relative_path,
                    }
                )
        max_lines = re.search(
            r"(?:max(?:imum)?\s+lines\s+per\s+file|max\s*lines/file|每(?:个)?文件.{0,12})"
            r"[^0-9]{0,20}(\d[\d,]*)",
            text,
            re.IGNORECASE,
        )
        if max_lines:
            constraints.setdefault("max_lines_per_file", int(max_lines.group(1).replace(",", "")))
        max_total = re.search(
            r"(?:max(?:imum)?\s+total\s+lines|总行数.{0,12})[^0-9]{0,20}[~≈]?\s*(\d[\d,]*)",
            text,
            re.IGNORECASE,
        )
        if max_total:
            constraints.setdefault("max_total_lines", int(max_total.group(1).replace(",", "")))
        checklist_rows = [
            int(match)
            for match in re.findall(r"(\d+)[-\s]?row", text, re.IGNORECASE)
        ]
        if checklist_rows:
            constraints["declared_checklist_rows"] = max(
                constraints.get("declared_checklist_rows", 0),
                max(checklist_rows),
            )
    result = {
        "format_files": format_files,
        "requirements": _dedupe(requirements),
        "template_markers": _dedupe(template_markers),
        "required_module_markers": _dedupe(required_module_markers),
        "section_file_mapping": section_file_mapping,
        "constraints": constraints,
    }
    return {key: value for key, value in result.items() if value}


def _normalize_structured_quality_contract(
    value: Any,
    *,
    source_file: str,
    diagnostics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Normalize a canonical, format-agnostic quality contract mapping."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        _diagnostic(
            diagnostics,
            "errors",
            "invalid_quality_contract",
            "quality_contract must be a mapping.",
            source_file=source_file,
        )
        return {}

    normalized: dict[str, Any] = {}
    list_aliases = {
        "narrative_rules": ("narrative_rules", "quality_rules"),
        "requirements": ("requirements",),
        "template_markers": ("template_markers",),
        "required_module_markers": (
            "required_module_markers",
            "required_markers",
        ),
        "required_section_ids": ("required_section_ids",),
    }
    limits = {
        "narrative_rules": 100,
        "requirements": 100,
        "template_markers": 60,
        "required_module_markers": 40,
        "required_section_ids": 60,
    }
    for canonical, aliases in list_aliases.items():
        alias = next((item for item in aliases if item in value), None)
        if alias is None:
            continue
        raw = value.get(alias)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_quality_contract_list",
                f"quality_contract.{canonical} must be a string or ordered string list.",
                field=f"quality_contract.{canonical}",
                source_file=source_file,
            )
            continue
        normalized[canonical] = _dedupe([
            str(item).strip()
            for item in _bounded_sequence(
                raw,
                limit=limits[canonical],
                diagnostics=diagnostics,
                field=f"quality_contract.{canonical}",
                source_file=source_file,
            )
            if isinstance(item, str) and str(item).strip()
        ])

    mappings = value.get("section_file_mapping")
    if "section_file_mapping" in value:
        if not isinstance(mappings, list):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_section_file_mapping",
                "quality_contract.section_file_mapping must be an ordered list.",
                source_file=source_file,
            )
        else:
            normalized["section_file_mapping"] = [
                _compact_mapping(
                    item,
                    max_items=40,
                    diagnostics=diagnostics,
                    field=f"quality_contract.section_file_mapping[{index}]",
                    source_file=source_file,
                )
                for index, item in enumerate(_bounded_sequence(
                    mappings,
                    limit=80,
                    diagnostics=diagnostics,
                    field="quality_contract.section_file_mapping",
                    source_file=source_file,
                ))
                if isinstance(item, dict)
            ]

    constraints = value.get("constraints")
    if "constraints" in value:
        if not isinstance(constraints, dict):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_quality_constraints",
                "quality_contract.constraints must be a mapping.",
                source_file=source_file,
            )
        else:
            normalized["constraints"] = _compact_mapping(
                constraints,
                max_items=80,
                diagnostics=diagnostics,
                field="quality_contract.constraints",
                source_file=source_file,
            )

    # Preserve the verifier's format-agnostic completion policies through the
    # compiler boundary.  These fields used to work only when callers invoked
    # ``verify_artifact_contract`` directly; a real structured Skill silently
    # lost them while producing the execution IR.  Keep the accepted surface
    # explicit and typed so arbitrary quality prose never becomes executable
    # or verifier authority.
    boolean_fields = (
        "forbid_pending_markers",
        "detect_padding",
        "forbid_duplicate_numbered_headings",
        "require_checklist_status",
        "allow_degraded_checklist",
        "require_merge_receipt",
    )
    for field_name in boolean_fields:
        if field_name not in value:
            continue
        raw = value.get(field_name)
        if isinstance(raw, bool):
            normalized[field_name] = raw
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_quality_contract_field",
                f"quality_contract.{field_name} must be a boolean.",
                field=f"quality_contract.{field_name}",
                source_file=source_file,
            )

    integer_fields = (
        "max_module_lines",
        "declared_checklist_rows",
    )
    for field_name in integer_fields:
        if field_name not in value:
            continue
        raw = value.get(field_name)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            normalized[field_name] = raw
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_quality_contract_field",
                f"quality_contract.{field_name} must be a non-negative integer.",
                field=f"quality_contract.{field_name}",
                source_file=source_file,
            )

    if "checklist_file" in value:
        raw_checklist_file = value.get("checklist_file")
        if (
            isinstance(raw_checklist_file, str)
            and _safe_structured_artifact_path(raw_checklist_file.strip())
        ):
            normalized["checklist_file"] = raw_checklist_file.strip()
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_quality_contract_field",
                "quality_contract.checklist_file must be one safe workspace-relative path.",
                field="quality_contract.checklist_file",
                source_file=source_file,
            )

    if "checklist_row_mode" in value:
        raw_row_mode = value.get("checklist_row_mode")
        row_mode = (
            raw_row_mode.strip().casefold().replace("-", "_")
            if isinstance(raw_row_mode, str)
            else ""
        )
        row_mode_aliases = {
            "exact": "exact",
            "minimum": "minimum",
            "min": "minimum",
            "at_least": "minimum",
        }
        if row_mode in row_mode_aliases:
            normalized["checklist_row_mode"] = row_mode_aliases[row_mode]
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_quality_contract_field",
                "quality_contract.checklist_row_mode must be exact or minimum.",
                field="quality_contract.checklist_row_mode",
                source_file=source_file,
            )

    if "pending_markers" in value:
        raw_pending = value.get("pending_markers")
        if isinstance(raw_pending, bool):
            normalized["pending_markers"] = raw_pending
        elif isinstance(raw_pending, list) and all(
            isinstance(item, str) and item
            for item in raw_pending
        ):
            normalized["pending_markers"] = _bounded_sequence(
                raw_pending,
                limit=64,
                diagnostics=diagnostics,
                field="quality_contract.pending_markers",
                source_file=source_file,
            )
        elif isinstance(raw_pending, dict):
            normalized["pending_markers"] = _compact_mapping(
                raw_pending,
                max_items=64,
                max_text=2_000,
                diagnostics=diagnostics,
                field="quality_contract.pending_markers",
                source_file=source_file,
            )
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_quality_contract_field",
                "quality_contract.pending_markers must be a boolean, string list, or mapping.",
                field="quality_contract.pending_markers",
                source_file=source_file,
            )

    checklist_declarations = [
        (field_name, value.get(field_name))
        for field_name in ("checklist", "checklist_validation")
        if field_name in value
    ]
    normalized_checklists: list[Any] = []
    for field_name, raw_checklist in checklist_declarations:
        if isinstance(raw_checklist, bool):
            candidate = raw_checklist
        elif isinstance(raw_checklist, dict):
            candidate = _compact_mapping(
                raw_checklist,
                max_items=64,
                max_text=4_000,
                diagnostics=diagnostics,
                field=f"quality_contract.{field_name}",
                source_file=source_file,
            )
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_quality_contract_field",
                f"quality_contract.{field_name} must be a boolean or mapping.",
                field=f"quality_contract.{field_name}",
                source_file=source_file,
            )
            continue
        normalized_checklists.append(candidate)
    if normalized_checklists:
        if any(item != normalized_checklists[0] for item in normalized_checklists[1:]):
            _diagnostic(
                diagnostics,
                "errors",
                "conflicting_quality_checklist_contract",
                "quality_contract.checklist and checklist_validation declare different policies.",
                field="quality_contract.checklist",
                source_file=source_file,
            )
        normalized["checklist"] = normalized_checklists[0]

    if "padding_policy" in value:
        raw_padding = value.get("padding_policy")
        if isinstance(raw_padding, dict):
            normalized["padding_policy"] = _compact_mapping(
                raw_padding,
                max_items=32,
                max_text=2_000,
                diagnostics=diagnostics,
                field="quality_contract.padding_policy",
                source_file=source_file,
            )
        else:
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_quality_contract_field",
                "quality_contract.padding_policy must be a mapping.",
                field="quality_contract.padding_policy",
                source_file=source_file,
            )
    return {
        key: item
        for key, item in normalized.items()
        if item not in (None, [], {})
    }


def _normalize_output_and_quality(
    orchestrators: list[tuple[str, dict[str, Any]]],
    output_contract: dict[str, Any],
    skill_dir: Path,
    diagnostics: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_output = dict(output_contract)
    sections: list[dict[str, Any]] = []
    narrative_rules: list[str] = []
    merge_declarations: list[dict[str, Any]] = []
    structured_output_by_field: dict[str, Any] = {}
    structured_output_source: dict[str, str] = {}
    declared_quality: dict[str, Any] = {}
    declared_quality_source: dict[str, str] = {}
    for source_file, orchestrator in orchestrators:
        structured_output = _normalize_structured_package_output_contract(
            orchestrator.get("output_contract"),
            skill_dir=skill_dir,
            source_file=source_file,
            diagnostics=diagnostics,
        )
        if structured_output:
            for field_name, field_value in structured_output.items():
                if field_name == "sections":
                    sections.extend(
                        item for item in field_value if isinstance(item, dict)
                    )
                    continue
                if field_name == "merge_declarations":
                    merge_declarations.extend(
                        item for item in field_value if isinstance(item, dict)
                    )
                    continue
                if field_name not in structured_output_by_field:
                    structured_output_by_field[field_name] = field_value
                    structured_output_source[field_name] = source_file
                    continue
                prior = structured_output_by_field[field_name]
                if prior == field_value:
                    continue
                if isinstance(prior, dict) and isinstance(field_value, dict):
                    conflicts = [
                        key for key in set(prior) & set(field_value)
                        if prior[key] != field_value[key]
                    ]
                    if not conflicts:
                        structured_output_by_field[field_name] = {
                            **prior,
                            **field_value,
                        }
                        continue
                _diagnostic(
                    diagnostics,
                    "errors",
                    "conflicting_structured_output_contract",
                    "Multiple orchestrators declare incompatible package output contract fields.",
                    field=f"output_contract.{field_name}",
                    first_source=structured_output_source[field_name],
                    conflicting_source=source_file,
                )
        current_quality = _normalize_structured_quality_contract(
            orchestrator.get("quality_contract"),
            source_file=source_file,
            diagnostics=diagnostics,
        )
        for field_name, field_value in current_quality.items():
            if field_name not in declared_quality:
                declared_quality[field_name] = field_value
                declared_quality_source[field_name] = source_file
                continue
            prior = declared_quality[field_name]
            if isinstance(prior, list) and isinstance(field_value, list):
                declared_quality[field_name] = _dedupe(prior + field_value)
                continue
            if isinstance(prior, dict) and isinstance(field_value, dict):
                conflicts = [
                    key for key in set(prior) & set(field_value)
                    if prior[key] != field_value[key]
                ]
                if not conflicts:
                    declared_quality[field_name] = {**prior, **field_value}
                    continue
            if prior != field_value:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "conflicting_structured_quality_contract",
                    "Multiple orchestrators declare incompatible quality contract fields.",
                    field=f"quality_contract.{field_name}",
                    first_source=declared_quality_source[field_name],
                    conflicting_source=source_file,
                )
        template = orchestrator.get("final_report_template")
        if not isinstance(template, dict):
            continue
        declared_sections = template.get("sections")
        if declared_sections is not None:
            sections.extend(_normalize_structured_sections(
                declared_sections,
                source_file=source_file,
                diagnostics=diagnostics,
                field="final_report_template.sections",
            ))
        merge = template.get("auto_merge") or template.get("merge")
        if isinstance(merge, dict):
            merge_declarations.append(
                {
                    key: _compact_mapping(
                        merge.get(key),
                        max_items=40,
                        diagnostics=diagnostics,
                        field=f"output_contract.merge.{key}",
                        source_file=source_file,
                    )
                    for key in (
                        "mandatory",
                        "trigger",
                        "command_template",
                        "command",
                        "output_artifact",
                        "expected_size_range",
                        "post_merge_verification",
                    )
                    if merge.get(key) is not None
                }
                | {"source_file": source_file}
            )
        narrative = (
            template.get("narrative_quality_instructions")
            or template.get("quality_rules")
            or template.get("narrative_rules")
        )
        narrative_rules.extend(_split_numbered_rules(narrative))
    if structured_output_by_field:
        normalized_output.update(structured_output_by_field)
    if sections:
        normalized_output["sections"] = sections
        normalized_output.setdefault("declared_section_count", len(sections))
        normalized_output.setdefault("section_titles", [section["title"] for section in sections])
    if merge_declarations:
        normalized_output["merge_declarations"] = merge_declarations

    output_list_limits = {
        "section_titles": 60,
        "post_merge_checks": 20,
        "declared_modular_files": 40,
        "declared_ancillary_files": 40,
        "declared_format_final_artifacts": 20,
        "artifact_indexes": 20,
        "artifact_set_policies": 20,
        "format_declarations": 20,
        "output_format_files": 20,
    }
    for field, limit in output_list_limits.items():
        value = normalized_output.get(field)
        if isinstance(value, list):
            normalized_output[field] = _bounded_sequence(
                value,
                limit=limit,
                diagnostics=diagnostics,
                field=f"output_contract.{field}",
            )

    format_quality = _parse_format_quality_contract(
        skill_dir,
        _as_string_list(output_contract.get("output_format_files")),
    )
    quality_contract = {
        "narrative_rules": _bounded_sequence(
            _dedupe(narrative_rules),
            limit=100,
            diagnostics=diagnostics,
            field="quality_contract.narrative_rules",
        ),
        "required_section_ids": [section["id"] for section in sections],
        **format_quality,
    }
    for field_name, field_value in declared_quality.items():
        prior = quality_contract.get(field_name)
        if isinstance(prior, list) and isinstance(field_value, list):
            quality_contract[field_name] = _dedupe(prior + field_value)
        elif isinstance(prior, dict) and isinstance(field_value, dict):
            conflicts = [
                key for key in set(prior) & set(field_value)
                if prior[key] != field_value[key]
            ]
            if conflicts:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "conflicting_inferred_quality_contract",
                    "A structured quality declaration conflicts with a referenced format declaration.",
                    field=f"quality_contract.{field_name}",
                    conflicting_keys=sorted(str(key) for key in conflicts),
                )
            else:
                quality_contract[field_name] = {**prior, **field_value}
        else:
            quality_contract[field_name] = field_value
    quality_list_limits = {
        "format_files": 20,
        "requirements": 100,
        "template_markers": 60,
        "required_module_markers": 40,
        "section_file_mapping": 80,
    }
    for field, limit in quality_list_limits.items():
        value = quality_contract.get(field)
        if isinstance(value, list):
            quality_contract[field] = _bounded_sequence(
                value,
                limit=limit,
                diagnostics=diagnostics,
                field=f"quality_contract.{field}",
            )
    return (
        {key: value for key, value in normalized_output.items() if value not in (None, [], {})},
        {key: value for key, value in quality_contract.items() if value not in (None, [], {})},
    )


def _normalize_section_label(value: Any) -> str:
    label = re.sub(r"^\s*\d+\s*[.)、:-]\s*", "", str(value or ""))
    return re.sub(r"[\W_]+", "", label, flags=re.UNICODE).casefold()


def _compile_section_file_mapping(
    output_contract: dict[str, Any],
    quality_contract: dict[str, Any],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> None:
    sections = [
        section
        for section in output_contract.get("sections") or []
        if isinstance(section, dict) and section.get("id")
    ]
    mappings = [
        mapping
        for mapping in quality_contract.get("section_file_mapping") or []
        if isinstance(mapping, dict)
    ]
    if not sections or not mappings:
        return

    by_order: dict[int, str] = {}
    by_label: dict[str, str] = {}
    section_ids: list[str] = []
    for section in sections:
        section_id = str(section["id"])
        section_ids.append(section_id)
        order = _parse_int_literal(section.get("order"))
        if order is not None:
            if order in by_order and by_order[order] != section_id:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "duplicate_section_order",
                    f"Final report template reuses section order {order}.",
                    section_order=order,
                    section_ids=[by_order[order], section_id],
                )
            else:
                by_order[order] = section_id
        for label in (section.get("id"), section.get("title")):
            normalized = _normalize_section_label(label)
            if normalized:
                by_label.setdefault(normalized, section_id)

    mapped_files: dict[str, list[str]] = {}
    for mapping in mappings:
        raw = str(mapping.get("sections") or mapping.get("raw_sections") or "").strip()
        mapping["raw_sections"] = raw
        resolved_ids: list[str] = []
        unresolved_segments: list[str] = []
        segments = [
            segment.strip()
            for segment in re.split(r"\s+\+\s+|[;\n；]+", raw)
            if segment.strip()
        ]
        for segment in segments:
            matched_segment = False
            referenced_orders = [
                int(match)
                for match in re.findall(r"(?<![\w.])(\d{1,3})\s*[.)、]", segment)
            ]
            if not referenced_orders and re.search(
                r"\bsections?\b|§|章节|第\s*\d+\s*节",
                segment,
                re.IGNORECASE,
            ):
                referenced_orders = [
                    int(match)
                    for match in re.findall(r"(?<![\w.])(\d{1,3})(?![\w.])", segment)
                ]
            for order in referenced_orders:
                section_id = by_order.get(order)
                if section_id:
                    resolved_ids.append(section_id)
                    matched_segment = True
                else:
                    _diagnostic(
                        diagnostics,
                        "warnings",
                        "unknown_section_order_reference",
                        f"Output mapping references undeclared report section order {order}.",
                        file=mapping.get("file"),
                        raw_section=segment,
                        section_order=order,
                        source_file=mapping.get("source_file"),
                    )
            if not matched_segment:
                normalized_segment = _normalize_section_label(segment)
                section_id = by_label.get(normalized_segment)
                if section_id:
                    resolved_ids.append(section_id)
                    matched_segment = True
            if not matched_segment:
                unresolved_segments.append(segment)

        mapping["section_ids"] = _dedupe(resolved_ids)
        if unresolved_segments:
            mapping["unresolved_sections"] = unresolved_segments
            # The format file remains authoritative for structural groups
            # that do not duplicate an orchestrator section. Keep those raw
            # groups independent instead of inventing canonical section IDs,
            # while still giving the verifier an exact minimum heading count.
            mapping["enforce_heading_count"] = True
            mapping["required_heading_groups"] = (
                len(mapping["section_ids"]) + len(unresolved_segments)
            )
            _diagnostic(
                diagnostics,
                "warnings",
                "unresolved_section_file_mapping",
                "Some format mapping entries could not be linked reliably to orchestrator sections.",
                file=mapping.get("file"),
                unresolved_sections=unresolved_segments,
                source_file=mapping.get("source_file"),
            )
        for section_id in mapping["section_ids"]:
            mapped_files.setdefault(section_id, []).append(str(mapping.get("file") or ""))

    for section_id, files in mapped_files.items():
        unique_files = _dedupe(files)
        if len(unique_files) > 1:
            _diagnostic(
                diagnostics,
                "warnings",
                "duplicate_section_file_mapping",
                f"Report section {section_id!r} is mapped to multiple output files.",
                section_id=section_id,
                files=unique_files,
            )
    unmapped = [section_id for section_id in section_ids if section_id not in mapped_files]
    if unmapped:
        _diagnostic(
            diagnostics,
            "warnings",
            "unmapped_report_sections",
            "Some orchestrator report sections are absent from the structured file mapping.",
            section_ids=unmapped,
        )


def _normalize_version(value: Any) -> str:
    return re.sub(r"^[vV]\s*", "", str(value or "").strip())


def _lint_worker_dependencies(
    workers: list[dict[str, Any]],
    worker_ids: set[str],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> None:
    graph: dict[str, list[str]] = {}
    for worker in workers:
        worker_id = str(worker.get("id") or "").strip()
        if not worker_id:
            continue
        graph.setdefault(worker_id, [])
        for dependency in worker.get("dependencies") or []:
            dependency = str(dependency).strip()
            if dependency not in worker_ids:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "missing_worker_dependency",
                    f"Worker {worker_id!r} depends on undeclared worker {dependency!r}.",
                    worker_id=worker_id,
                    dependency=dependency,
                    file=worker.get("file"),
                )
                continue
            graph[worker_id].append(dependency)
    for cycle in _find_dependency_cycles(graph):
        _diagnostic(
            diagnostics,
            "errors",
            "worker_dependency_cycle",
            "Worker dependency declarations contain a cycle.",
            cycle=cycle,
        )


def _dependency_ancestor_ids(
    graph: dict[str, list[str]],
    node_id: str,
) -> set[str]:
    """Return the transitive dependency ancestors of one DAG node."""
    ancestors: set[str] = set()
    pending = list(graph.get(node_id) or [])
    while pending:
        dependency = str(pending.pop()).strip()
        if not dependency or dependency in ancestors:
            continue
        ancestors.add(dependency)
        pending.extend(graph.get(dependency) or [])
    return ancestors


def _lint_route_worker_dependencies(
    routes: list[dict[str, Any]],
    workers: list[dict[str, Any]],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> None:
    """Cross-check worker dependencies against each route's concrete waves.

    The worker graph and wave graph can each be acyclic while their combination
    is impossible to execute (for example, a worker sharing a parallel wave
    with its prerequisite).  Every routed worker therefore has exactly one wave
    assignment, and each dependency worker must live in a strict ancestor wave.
    """
    worker_dependencies = {
        str(worker.get("id") or "").strip(): _as_string_list(
            worker.get("dependencies")
        )
        for worker in workers
        if str(worker.get("id") or "").strip()
    }
    for route in routes:
        route_id = str(route.get("id") or "").strip()
        source_file = str(route.get("source_file") or "").strip()
        waves = [
            wave for wave in (route.get("waves") or [])
            if isinstance(wave, dict)
        ]
        wave_order = {
            str(wave.get("id") or "").strip(): index
            for index, wave in enumerate(waves)
            if str(wave.get("id") or "").strip()
        }
        wave_graph = {
            wave_id: [
                dependency for dependency in _as_string_list(wave.get("dependencies"))
                if dependency in wave_order
            ]
            for wave in waves
            for wave_id in [str(wave.get("id") or "").strip()]
            if wave_id
        }
        worker_waves: dict[str, list[str]] = {}
        for wave in waves:
            wave_id = str(wave.get("id") or "").strip()
            if not wave_id:
                continue
            for worker_id in _as_string_list(wave.get("workers")):
                worker_waves.setdefault(worker_id, []).append(wave_id)

        routed_workers = _as_string_list(route.get("workers"))
        for worker_id in routed_workers:
            assignments = worker_waves.get(worker_id) or []
            unique_assignments = _dedupe(assignments)
            if not unique_assignments:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "route_worker_missing_wave_assignment",
                    f"Route {route_id!r} references worker {worker_id!r} but assigns it to no wave.",
                    route_id=route_id,
                    worker_id=worker_id,
                    orchestrator_file=source_file,
                )
                continue
            if len(unique_assignments) > 1:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "duplicate_worker_wave_assignment",
                    f"Route {route_id!r} assigns worker {worker_id!r} to more than one wave.",
                    route_id=route_id,
                    worker_id=worker_id,
                    wave_ids=unique_assignments,
                    orchestrator_file=source_file,
                )
                continue

            worker_wave = unique_assignments[0]
            worker_ancestors = _dependency_ancestor_ids(wave_graph, worker_wave)
            for dependency_id in worker_dependencies.get(worker_id) or []:
                dependency_assignments = _dedupe(
                    worker_waves.get(dependency_id) or []
                )
                if not dependency_assignments:
                    _diagnostic(
                        diagnostics,
                        "errors",
                        "worker_dependency_missing_from_route",
                        f"Route {route_id!r} dispatches worker {worker_id!r} without its dependency {dependency_id!r}.",
                        route_id=route_id,
                        worker_id=worker_id,
                        dependency=dependency_id,
                        worker_wave=worker_wave,
                        orchestrator_file=source_file,
                    )
                    continue
                if len(dependency_assignments) > 1:
                    # The duplicate assignment already makes the route invalid;
                    # avoid choosing an arbitrary dependency wave here.
                    continue
                dependency_wave = dependency_assignments[0]
                if dependency_wave == worker_wave:
                    _diagnostic(
                        diagnostics,
                        "errors",
                        "worker_dependency_same_wave",
                        f"Route {route_id!r} places worker {worker_id!r} in the same wave as dependency {dependency_id!r}.",
                        route_id=route_id,
                        worker_id=worker_id,
                        dependency=dependency_id,
                        worker_wave=worker_wave,
                        orchestrator_file=source_file,
                    )
                    continue
                if dependency_wave in worker_ancestors:
                    continue
                dependency_ancestors = _dependency_ancestor_ids(
                    wave_graph, dependency_wave
                )
                dependency_is_later = (
                    worker_wave in dependency_ancestors
                    or wave_order.get(dependency_wave, -1)
                    > wave_order.get(worker_wave, -1)
                )
                code = (
                    "worker_dependency_later_wave"
                    if dependency_is_later
                    else "worker_dependency_wave_not_ancestor"
                )
                _diagnostic(
                    diagnostics,
                    "errors",
                    code,
                    f"Route {route_id!r} does not place dependency {dependency_id!r} in a strict ancestor wave of worker {worker_id!r}.",
                    route_id=route_id,
                    worker_id=worker_id,
                    dependency=dependency_id,
                    worker_wave=worker_wave,
                    dependency_wave=dependency_wave,
                    orchestrator_file=source_file,
                )


def _lint_aggregation_dependencies(
    aggregation: dict[str, Any],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> None:
    """Validate aggregation IDs and the executable aggregation DAG."""
    steps = [
        step for step in (aggregation.get("steps") or [])
        if isinstance(step, dict) and str(step.get("id") or "").strip()
    ]
    seen_ids: set[str] = set()
    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if step_id in seen_ids:
            _diagnostic(
                diagnostics,
                "errors",
                "duplicate_aggregation_step_id",
                f"Aggregation step id {step_id!r} is declared more than once.",
                step_id=step_id,
                orchestrator_file=step.get("source_file"),
            )
        seen_ids.add(step_id)

    runnable_steps = [step for step in steps if step.get("required") is not False]
    runnable_ids = {
        str(step.get("id") or "").strip() for step in runnable_steps
    }
    graph: dict[str, list[str]] = {}
    for step in runnable_steps:
        step_id = str(step.get("id") or "").strip()
        graph.setdefault(step_id, [])
        for dependency in _as_string_list(step.get("depends_on")):
            if dependency not in runnable_ids:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "missing_aggregation_dependency",
                    f"Aggregation step {step_id!r} depends on an undeclared or non-runnable step {dependency!r}.",
                    step_id=step_id,
                    dependency=dependency,
                    orchestrator_file=step.get("source_file"),
                )
                continue
            graph[step_id].append(dependency)
    for cycle in _find_dependency_cycles(graph):
        _diagnostic(
            diagnostics,
            "errors",
            "aggregation_dependency_cycle",
            "Aggregation step dependency declarations contain a cycle.",
            cycle=cycle,
        )


def _lint_output_contract(
    output_contract: dict[str, Any],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> None:
    declarations = [
        declaration
        for declaration in output_contract.get("format_declarations") or []
        if isinstance(declaration, dict)
    ]
    artifact_indexes = _as_string_list(output_contract.get("artifact_indexes"))
    if len(artifact_indexes) > 1:
        _diagnostic(
            diagnostics,
            "warnings",
            "multiple_artifact_indexes",
            "Output packaging documents label more than one artifact as the package index.",
            artifacts=artifact_indexes,
        )
    exact_policies = [
        policy
        for policy in output_contract.get("artifact_set_policies") or []
        if isinstance(policy, dict)
    ]
    if len(exact_policies) > 1:
        _diagnostic(
            diagnostics,
            "warnings",
            "multiple_exact_artifact_set_policies",
            "Multiple exact artifact-set policies are declared; runtime selection may be ambiguous.",
            source_files=[
                policy.get("source_file")
                for policy in exact_policies
            ],
        )
    declared_totals = {
        int(declaration["declared_file_count"])
        for declaration in declarations
        if isinstance(declaration.get("declared_file_count"), int)
    }
    if len(declared_totals) > 1:
        _diagnostic(
            diagnostics,
            "errors",
            "conflicting_file_count_declarations",
            "Selected output packaging documents declare different total file counts.",
            declared_file_counts=sorted(declared_totals),
            source_files=[
                declaration.get("source_file")
                for declaration in declarations
                if declaration.get("declared_file_count") is not None
            ],
        )

    modular_files = _as_string_list(output_contract.get("declared_modular_files"))
    declared_modular_count = _parse_int_literal(
        output_contract.get("declared_modular_file_count")
    )
    if (
        declared_modular_count is not None
        and modular_files
        and declared_modular_count != len(modular_files)
    ):
        _diagnostic(
            diagnostics,
            "errors",
            "declared_modular_file_count_mismatch",
            "Declared modular file count does not match the explicitly listed modular files.",
            declared_count=declared_modular_count,
            listed_count=len(modular_files),
        )

    modular_sources: dict[str, list[str]] = {}
    for declaration in declarations:
        source_file = str(declaration.get("source_file") or "")
        for filename in _as_string_list(declaration.get("modular_files")):
            modular_sources.setdefault(filename, []).append(source_file)
    for filename, source_files in modular_sources.items():
        unique_sources = _dedupe(source_files)
        if len(unique_sources) > 1:
            _diagnostic(
                diagnostics,
                "warnings",
                "duplicate_output_artifact_declaration",
                f"Modular output artifact {filename!r} is declared by multiple packaging documents.",
                artifact=filename,
                source_files=unique_sources,
            )

    for declaration in declarations:
        total_count = _parse_int_literal(declaration.get("declared_file_count"))
        if total_count is None:
            continue
        listed_modules = _as_string_list(declaration.get("modular_files"))
        modular_count = _parse_int_literal(
            declaration.get("declared_modular_file_count")
        )
        if modular_count is not None and listed_modules and modular_count != len(listed_modules):
            _diagnostic(
                diagnostics,
                "errors",
                "declared_modular_file_count_mismatch",
                "A packaging document's modular count does not match its listed files.",
                source_file=declaration.get("source_file"),
                declared_count=modular_count,
                listed_count=len(listed_modules),
            )
        known_modular_count = modular_count if modular_count is not None else len(listed_modules)
        ancillary_count = len(_as_string_list(declaration.get("ancillary_files")))
        final_count = 1 if _as_string_list(declaration.get("final_artifacts")) else 0
        accounted_count = known_modular_count + ancillary_count + final_count
        if accounted_count == total_count:
            continue
        context = {
            "source_file": declaration.get("source_file"),
            "declared_total": total_count,
            "accounted_total": accounted_count,
            "modular_count": known_modular_count,
            "ancillary_count": ancillary_count,
            "final_artifact_count": final_count,
        }
        if accounted_count > total_count or (
            modular_count is not None and (ancillary_count or final_count)
        ):
            _diagnostic(
                diagnostics,
                "errors",
                "file_count_arithmetic_mismatch",
                "Output package file-count arithmetic is inconsistent.",
                **context,
            )
        else:
            _diagnostic(
                diagnostics,
                "warnings",
                "file_count_not_fully_accounted",
                "Output package total includes files that are not explicitly identified.",
                **context,
            )


_ENVIRONMENT_REQUIREMENT_KEYS = frozenset({
    "command", "commands", "bin", "bins", "executables",
    "env", "env_vars", "environment", "environment_variables",
    "package", "packages", "python_packages", "pip", "pip_packages",
    "dependencies", "platform", "platforms",
})
_DECLARED_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_DECLARED_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DECLARED_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?.{0,420}$")
_DECLARED_TOOL_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}(?:\([^()\r\n]{1,256}\))?$"
)


def _environment_named_items(
    value: Any,
    *,
    name_keys: tuple[str, ...],
    default_optional: bool = False,
) -> list[tuple[str, bool]]:
    """Normalize a scalar/list/mapping requirement without executing it."""
    if value is None:
        return []
    if isinstance(value, str):
        return [(value.strip(), default_optional)] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        items: list[tuple[str, bool]] = []
        for entry in value:
            if isinstance(entry, dict):
                name = next(
                    (
                        str(entry.get(key)).strip()
                        for key in name_keys
                        if entry.get(key) is not None
                        and str(entry.get(key)).strip()
                    ),
                    "",
                )
                if name:
                    items.append((name, bool(entry.get("optional", default_optional))))
            elif entry is not None and str(entry).strip():
                items.append((str(entry).strip(), default_optional))
        return items
    if isinstance(value, dict):
        # A single record (for example ``{name: TOKEN, optional: true}``).
        record_name = next(
            (
                str(value.get(key)).strip()
                for key in name_keys
                if value.get(key) is not None and str(value.get(key)).strip()
            ),
            "",
        )
        if record_name:
            return [(record_name, bool(value.get("optional", default_optional)))]
        items = []
        for name, config in value.items():
            if config in (False, None):
                continue
            optional = default_optional
            if isinstance(config, dict):
                optional = bool(config.get("optional", default_optional))
            items.append((str(name).strip(), optional))
        return [(name, optional) for name, optional in items if name]
    return [(str(value).strip(), default_optional)] if str(value).strip() else []


def _package_requirement_items(value: Any) -> list[tuple[str, bool]]:
    if isinstance(value, dict) and not any(
        key in value for key in ("name", "package", "requirement", "id")
    ):
        items: list[tuple[str, bool]] = []
        for name, spec in value.items():
            if spec in (False, None):
                continue
            optional = False
            if isinstance(spec, dict):
                optional = bool(spec.get("optional", False))
                version = spec.get("version") or spec.get("specifier") or ""
            else:
                version = spec if isinstance(spec, str) else ""
            requirement = str(name).strip()
            version_text = str(version).strip()
            if version_text and version_text not in {"*", "true"}:
                requirement += (
                    version_text
                    if version_text.startswith(("=", "<", ">", "!", "~"))
                    else "==" + version_text
                )
            if requirement:
                items.append((requirement, optional))
        return items
    return _environment_named_items(
        value,
        name_keys=("requirement", "package", "name", "id"),
    )


def _allowed_tool_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return parse_allowed_tool_selectors(value)
    if isinstance(value, (list, tuple, set)):
        return [
            selector
            for item in value
            for selector in (
                parse_allowed_tool_selectors(item)
                if isinstance(item, str) else []
            )
        ]
    if isinstance(value, dict):
        return [
            str(name).strip()
            for name, enabled in value.items()
            if enabled not in (False, None) and str(name).strip()
        ]
    return []


def _normalize_platform(value: str) -> str:
    normalized = str(value or "").strip().casefold().replace("_", "-")
    aliases = {
        "darwin": "macos",
        "mac": "macos",
        "mac-os": "macos",
        "osx": "macos",
        "win": "windows",
        "win32": "windows",
        "cygwin": "windows",
        "linux2": "linux",
    }
    return aliases.get(normalized, normalized)


def _compile_environment_contract(
    *,
    frontmatter: dict[str, Any],
    skill_body: str,
    orchestrators: list[tuple[str, dict[str, Any]]],
    diagnostics: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Compile declarative activation prerequisites into a bounded IR.

    This preflight contract never installs software, launches a command, or
    reads secret values.  It only retains explicit machine-readable
    declarations; prose setup guidance remains in the full Skill body and is
    deliberately not guessed into executable requirements.
    """
    declarations: list[tuple[str, dict[str, Any]]] = [("SKILL.md", frontmatter)]
    declarations.extend(orchestrators)
    commands: list[dict[str, Any]] = []
    environment_variables: list[dict[str, Any]] = []
    packages: list[dict[str, Any]] = []
    platform_groups: list[dict[str, Any]] = []
    allowed_tool_groups: list[dict[str, Any]] = []

    def add_named(
        target: list[dict[str, Any]],
        values: list[tuple[str, bool]],
        *,
        kind: str,
        source_file: str,
        field: str,
        pattern: re.Pattern[str],
    ) -> None:
        bounded = _bounded_sequence(
            values,
            limit=MAX_ENVIRONMENT_CONTRACT_ITEMS,
            diagnostics=diagnostics,
            field=field,
            source_file=source_file,
        )
        for name, optional in bounded:
            value = str(name).strip()
            invalid_package = kind == "package" and (
                any(token in value for token in ("://", "@", "\\", "\x00", "\n", "\r", "`", "$", "|"))
                or value.startswith((".", "/", "git+", "file:"))
            )
            if (
                not value
                or len(value) > 500
                or invalid_package
                or pattern.fullmatch(value) is None
            ):
                _diagnostic(
                    diagnostics,
                    "errors",
                    f"invalid_{kind}_prerequisite",
                    f"A declared {kind} prerequisite has an unsafe or unsupported syntax.",
                    source_file=source_file,
                    field=field,
                    declaration=value[:200],
                )
                continue
            target.append({
                "name" if kind != "package" else "requirement": value,
                "optional": bool(optional),
                "source_file": source_file,
                "field": field,
            })

    def collect_prerequisites(
        value: Any,
        *,
        source_file: str,
        field: str,
    ) -> None:
        if value in (None, [], {}):
            return
        if not isinstance(value, dict):
            _diagnostic(
                diagnostics,
                "errors",
                "invalid_prerequisites_contract",
                "Prerequisites/requirements must be a mapping with explicit commands, environment, packages, or platforms.",
                source_file=source_file,
                field=field,
            )
            return
        for unsupported in sorted(
            str(key) for key in value if str(key) not in _ENVIRONMENT_REQUIREMENT_KEYS
        ):
            _diagnostic(
                diagnostics,
                "errors",
                "unsupported_prerequisite_field",
                "A prerequisite field has no safe activation-preflight implementation.",
                source_file=source_file,
                field=f"{field}.{unsupported}",
            )
        for key in ("command", "commands", "bin", "bins", "executables"):
            add_named(
                commands,
                _environment_named_items(value.get(key), name_keys=("command", "name", "id")),
                kind="command",
                source_file=source_file,
                field=f"{field}.{key}",
                pattern=_DECLARED_COMMAND_RE,
            )
        for key in ("env", "env_vars", "environment", "environment_variables"):
            add_named(
                environment_variables,
                _environment_named_items(value.get(key), name_keys=("name", "env", "variable", "id")),
                kind="environment_variable",
                source_file=source_file,
                field=f"{field}.{key}",
                pattern=_DECLARED_ENV_RE,
            )
        for key in (
            "package", "packages", "python_packages", "pip", "pip_packages",
            "dependencies",
        ):
            add_named(
                packages,
                _package_requirement_items(value.get(key)),
                kind="package",
                source_file=source_file,
                field=f"{field}.{key}",
                pattern=_DECLARED_PACKAGE_RE,
            )
        for key in ("platform", "platforms"):
            raw_platforms = _as_string_list(value.get(key))
            if not raw_platforms:
                continue
            normalized = [_normalize_platform(item) for item in raw_platforms]
            invalid = [
                item for item in normalized
                if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", item)
            ]
            if invalid:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_platform_prerequisite",
                    "A platform prerequisite has unsafe or unsupported syntax.",
                    source_file=source_file,
                    field=f"{field}.{key}",
                    declarations=invalid[:20],
                )
                continue
            platform_groups.append({
                "allowed": _dedupe(normalized),
                "source_file": source_file,
                "field": f"{field}.{key}",
            })

    for source_file, declaration in declarations:
        for key in ("allowed-tools", "allowed_tools"):
            if declaration.get(key) is None:
                continue
            raw_tools = _allowed_tool_items(declaration.get(key))
            raw_tools = [str(item) for item in _bounded_sequence(
                raw_tools,
                limit=MAX_ENVIRONMENT_CONTRACT_ITEMS,
                diagnostics=diagnostics,
                field=key,
                source_file=source_file,
            )]
            invalid = [item for item in raw_tools if _DECLARED_TOOL_RE.fullmatch(item) is None]
            if invalid:
                _diagnostic(
                    diagnostics,
                    "errors",
                    "invalid_allowed_tool_selector",
                    "An allowed-tools selector has unsafe or unsupported syntax.",
                    source_file=source_file,
                    field=key,
                    declarations=invalid[:20],
                )
            valid_tools = [item for item in raw_tools if item not in invalid]
            if valid_tools or not raw_tools:
                allowed_tool_groups.append({
                    "selectors": _dedupe(valid_tools),
                    "explicit_empty": not valid_tools,
                    "source_file": source_file,
                    "field": key,
                })

        platforms = _as_string_list(declaration.get("platforms"))
        if platforms:
            collect_prerequisites(
                {"platforms": platforms},
                source_file=source_file,
                field="activation",
            )
        if declaration.get("dependencies") is not None:
            dependency_value = declaration.get("dependencies")
            collect_prerequisites(
                dependency_value
                if isinstance(dependency_value, dict)
                else {"packages": dependency_value},
                source_file=source_file,
                field="dependencies",
            )
        for key in ("prerequisites", "requirements"):
            collect_prerequisites(
                declaration.get(key),
                source_file=source_file,
                field=key,
            )
        required_env = declaration.get("required_environment_variables")
        if required_env is not None:
            add_named(
                environment_variables,
                _environment_named_items(
                    required_env,
                    name_keys=("name", "env", "variable", "id"),
                ),
                kind="environment_variable",
                source_file=source_file,
                field="required_environment_variables",
                pattern=_DECLARED_ENV_RE,
            )

        metadata = declaration.get("metadata")
        hermes = metadata.get("hermes") if isinstance(metadata, dict) else None
        if isinstance(hermes, dict):
            collect_prerequisites(
                hermes.get("prerequisites"),
                source_file=source_file,
                field="metadata.hermes.prerequisites",
            )

    def merge_named(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for item in items:
            identity = str(item.get(key) or "").casefold()
            if not identity:
                continue
            if identity not in merged:
                merged[identity] = dict(item, source_files=[item.get("source_file")])
                merged[identity].pop("source_file", None)
                order.append(identity)
            else:
                current = merged[identity]
                # Any required declaration wins over an optional one.
                current["optional"] = bool(current.get("optional")) and bool(item.get("optional"))
                current["source_files"] = _dedupe([
                    *(current.get("source_files") or []), item.get("source_file"),
                ])
        return [merged[identity] for identity in order]

    prose_requirement_headings = bool(re.search(
        r"^#{1,6}\s+(?:installation(?:\s+and\s+setup)?|setup|requirements?|"
        r"prerequisites?|environment(?:\s+setup)?)\s*$",
        str(skill_body or ""),
        re.IGNORECASE | re.MULTILINE,
    ))
    if prose_requirement_headings:
        _diagnostic(
            diagnostics,
            "info",
            "prose_prerequisites_preserved_not_inferred",
            (
                "SKILL.md contains prose setup/prerequisite guidance. The full "
                "body is preserved for instruction following, but prose is not "
                "guessed into executable activation checks; use structured "
                "prerequisites/dependencies for fail-closed preflight."
            ),
            source_file="SKILL.md",
        )

    contract = {
        "schema_version": 1,
        "commands": merge_named(commands, "name"),
        "environment_variables": merge_named(environment_variables, "name"),
        "packages": merge_named(packages, "requirement"),
        "platform_groups": platform_groups,
        "allowed_tool_groups": allowed_tool_groups,
        "allowed_tools": _dedupe([
            selector
            for group in allowed_tool_groups
            for selector in group.get("selectors") or []
        ]),
        "prose_prerequisites": (
            {
                "detected": True,
                "machine_inferred": False,
                "source_file": "SKILL.md",
            }
            if prose_requirement_headings else None
        ),
    }
    contract["command_grants"] = compile_environment_command_grants(
        contract["commands"], contract["allowed_tools"]
    )
    normalized_contract = {
        key: value for key, value in contract.items()
        if value not in (None, [], {})
    }
    if set(normalized_contract) == {"schema_version"}:
        return {}
    return normalized_contract


def _compile_nested_environment_contract(
    declaration: dict[str, Any],
    *,
    source_file: str | None,
    diagnostics: dict[str, list[dict[str, Any]]],
    include_dependencies: bool = False,
) -> dict[str, Any]:
    """Compile activation fields embedded in one worker/source/step.

    Worker ``dependencies`` already means DAG worker IDs, so callers must opt
    in before that ambiguous key is interpreted as package requirements.
    """
    keys = {
        "allowed-tools",
        "allowed_tools",
        "platforms",
        "prerequisites",
        "requirements",
        "required_environment_variables",
        "metadata",
    }
    if include_dependencies:
        keys.add("dependencies")
    filtered = {
        key: declaration.get(key)
        for key in keys
        if declaration.get(key) is not None
    }
    if not filtered:
        return {}
    return _compile_environment_contract(
        frontmatter={},
        skill_body="",
        orchestrators=[(str(source_file or "declaration"), filtered)],
        diagnostics=diagnostics,
    )


def _compile_execution_contract(
    *,
    skill_dir: Path,
    worker_files: list[str],
    output_contract: dict[str, Any],
    frontmatter: dict[str, Any],
    skill_body: str,
    authorized_resources: set[str] | None = None,
) -> dict[str, Any]:
    """Compile package resources into a generic, machine-readable execution contract."""
    diagnostics: dict[str, list[dict[str, Any]]] = {"errors": [], "warnings": [], "info": []}
    orchestrators: list[tuple[str, dict[str, Any]]] = []
    for path in _orchestrator_yaml_candidates(skill_dir):
        source_file = str(path.relative_to(skill_dir))
        if (
            authorized_resources is not None
            and source_file not in authorized_resources
        ):
            continue
        parsed = _load_yaml_mapping(path, skill_dir, diagnostics, kind="orchestrator")
        if parsed is None:
            continue
        if not _looks_like_orchestrator(parsed):
            continue
        orchestrators.append((source_file, parsed))
        for unsupported_field in _unsupported_orchestrator_execution_fields(parsed):
            _diagnostic(
                diagnostics,
                "errors",
                "unsupported_execution_field",
                (
                    "A declarative Skill contains an execution-control field "
                    "with no lossless compiler/runtime representation. The "
                    "execution contract is invalid rather than silently "
                    "ignoring that control."
                ),
                field=unsupported_field,
                source_file=source_file,
                yaml_path=unsupported_field,
                disposition="fail_closed",
            )

    environment_contract = _compile_environment_contract(
        frontmatter=frontmatter,
        skill_body=skill_body,
        orchestrators=orchestrators,
        diagnostics=diagnostics,
    )

    if (
        not orchestrators
        and not worker_files
        and not output_contract
        and not environment_contract
        and not diagnostics["errors"]
    ):
        return {}

    workers = _discover_declared_workers(
        skill_dir,
        worker_files,
        orchestrators,
        diagnostics,
    )
    worker_ids = {str(worker.get("id")) for worker in workers if worker.get("id")}
    routes = _normalize_routes(skill_dir, orchestrators, worker_ids, diagnostics)
    route_selection_policy = _normalize_route_selection_policy(
        orchestrators,
        {str(route.get("id")) for route in routes if route.get("id")},
        diagnostics,
    )

    _lint_worker_dependencies(workers, worker_ids, diagnostics)
    _lint_route_worker_dependencies(routes, workers, diagnostics)

    orchestrator_metadata: list[dict[str, Any]] = []
    declared_versions: list[tuple[str, str]] = []
    skill_version = frontmatter.get("version")
    if skill_version:
        declared_versions.append(("SKILL.md", _normalize_version(skill_version)))
    for source_file, orchestrator in orchestrators:
        metadata = {
            "id": str(orchestrator.get("orchestrator_id") or orchestrator.get("id") or "").strip(),
            "name": str(orchestrator.get("name") or "").strip(),
            "version": str(orchestrator.get("version") or "").strip(),
            "description": _bounded_text(
                orchestrator.get("description"),
                limit=2_000,
                diagnostics=diagnostics,
                field="orchestrator.description",
                source_file=source_file,
            ),
            "source_file": source_file,
        }
        orchestrator_metadata.append(
            {key: value for key, value in metadata.items() if value not in ("", None)}
        )
        if orchestrator.get("version"):
            normalized_version = _normalize_version(orchestrator.get("version"))
            declared_versions.append((source_file, normalized_version))
            description = str(orchestrator.get("description") or "")
            described_versions = {
                _normalize_version(match)
                for match in re.findall(
                    r"\bv(?:ersion\s*)?(\d+(?:\.\d+){0,3})(?![\d.])",
                    description,
                    re.IGNORECASE,
                )
            }
            for described_version in described_versions:
                if described_version and described_version != normalized_version:
                    _diagnostic(
                        diagnostics,
                        "warnings",
                        "description_version_mismatch",
                        "Orchestrator description and version field declare different versions.",
                        orchestrator_file=source_file,
                        declared_version=normalized_version,
                        described_version=described_version,
                    )
    unique_versions = _dedupe([version for _, version in declared_versions if version])
    if len(unique_versions) > 1:
        _diagnostic(
            diagnostics,
            "warnings",
            "version_mismatch",
            "Skill and orchestrator metadata declare inconsistent versions.",
            declarations=[{"source": source, "version": version} for source, version in declared_versions],
        )

    normalized_output, quality_contract = _normalize_output_and_quality(
        orchestrators,
        output_contract,
        skill_dir,
        diagnostics,
    )
    _compile_section_file_mapping(
        normalized_output,
        quality_contract,
        diagnostics,
    )
    _lint_output_contract(normalized_output, diagnostics)
    intent_classification = _normalize_intent_classification(
        skill_dir,
        orchestrators,
        worker_ids,
        diagnostics,
    )
    knowledge_bootstrap = _normalize_bootstrap(
        skill_dir,
        orchestrators,
        diagnostics,
    )
    aggregation = _normalize_aggregation(
        skill_dir,
        orchestrators,
        diagnostics,
    )
    _lint_aggregation_dependencies(aggregation, diagnostics)
    conflict_resolution = _normalize_conflict_resolution(orchestrators, diagnostics)
    _refresh_diagnostic_summary(diagnostics)
    metadata = {
        "skill_name": str(frontmatter.get("name") or skill_dir.name),
        "skill_version": str(skill_version) if skill_version is not None else None,
        "orchestrators": orchestrator_metadata,
        "declared_versions": [
            {"source": source, "version": version}
            for source, version in declared_versions
        ],
    }
    contract = {
        "schema_version": 1,
        "skill_root": str(skill_dir),
        "source_files": [source_file for source_file, _ in orchestrators],
        "metadata": {
            key: value for key, value in metadata.items() if value not in (None, "", [], {})
        },
        "workers": workers,
        "worker_ids": sorted(worker_ids),
        "routes": routes,
        "route_selection_policy": route_selection_policy,
        "intent_classification": intent_classification,
        "knowledge_bootstrap": knowledge_bootstrap,
        "aggregation": aggregation,
        "conflict_resolution": conflict_resolution,
        "output_contract": normalized_output,
        "quality_contract": quality_contract,
        "environment_contract": environment_contract,
        "diagnostics": diagnostics,
    }
    return {key: value for key, value in contract.items() if value not in (None, [], {})}


_EXECUTION_EXTENSION_KINDS = frozenset({
    "chatds.orchestrator",
    "chatds.worker",
    "hermes.orchestrator",
    "hermes.worker",
    "openclaw.orchestrator",
    "openclaw.worker",
})

_BLOCKING_RESOURCE_DIRECTORIES = frozenset({
    "orchestration",
    "orchestrator",
    "workflows",
    "workers",
    "formats",
    "scripts",
    "evaluation",
})


def _resource_has_blocking_execution_role(relative_path: str) -> bool:
    """Return whether a normal SKILL.md link may grant compiler authority.

    References, examples, assets, templates, protocols, and ordinary root
    documents are progressive-disclosure context even when linked.  They can
    become blocking only through an explicit versioned/namespaced extension,
    never through their prose or directory placement alone.
    """
    path = PurePosixPath(relative_path)
    parts = path.parts
    if parts and parts[0].casefold() in _BLOCKING_RESOURCE_DIRECTORIES:
        return True
    if len(parts) != 1:
        return False
    suffix = path.suffix.casefold()
    if suffix in {".py", ".sh", ".bash", ".js", ".mjs"}:
        return True
    stem = path.stem.casefold()
    return bool(
        suffix in {".yaml", ".yml"}
        and (stem.startswith("orchestrat") or stem.startswith("worker"))
    )


def _normalize_authority_resource_reference(
    skill_dir: Path,
    known_resources: set[str],
    value: Any,
    *,
    source_file: str | None = None,
) -> str | None:
    """Resolve one exact package-file reference for authority propagation.

    Authority declarations are intentionally narrower than general prose path
    discovery: only a scalar that consists of one local file path (plus an
    optional Markdown anchor) can elevate another resource.  Output prose,
    commands, URLs, and wildcard examples therefore remain data.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip("`'\"")
    if not candidate or any(char.isspace() for char in candidate):
        return None
    candidate = candidate.split("#", 1)[0].strip()
    if candidate.startswith("./"):
        candidate = candidate[2:]
    if not candidate or "://" in candidate or candidate.startswith(("skill:", "/")):
        return None

    attempts = [candidate]
    if source_file:
        parent = PurePosixPath(source_file).parent
        if str(parent) not in {"", "."}:
            attempts.append(str(parent / candidate))
    for attempt in _dedupe(attempts):
        checked = validate_skill_resource(
            skill_dir,
            attempt,
            expected_kind="file",
            require_relative=True,
        )
        if not checked.valid or checked.path is None:
            continue
        relative = str(checked.path.relative_to(skill_dir))
        if relative in known_resources:
            return relative
    return None


def _skill_body_mentions_resource(skill_body: str, relative_path: str) -> bool:
    """Return whether SKILL.md names one exact local resource path."""
    if relative_path not in skill_body:
        return False
    pattern = re.compile(
        r"(?<![A-Za-z0-9_./-])(?:\./)?"
        + re.escape(relative_path)
        + r"(?:#[^\s)`>\]]*)?(?![A-Za-z0-9_./-])"
    )
    return pattern.search(skill_body) is not None


def _iter_bounded_scalar_strings(value: Any) -> list[str]:
    """Collect scalar strings from an already graph-audited YAML mapping."""
    result: list[str] = []
    pending = [value]
    visited: set[int] = set()
    while pending and len(visited) <= MAX_COMPILER_STRUCTURE_NODES:
        node = pending.pop()
        if isinstance(node, str):
            result.append(node)
            continue
        if not isinstance(node, (dict, list, tuple)):
            continue
        identity = id(node)
        if identity in visited:
            continue
        visited.add(identity)
        if isinstance(node, dict):
            pending.extend(node.values())
        else:
            pending.extend(node)
    return result


def _is_explicit_execution_extension(data: dict[str, Any]) -> bool:
    """Recognize an explicit harness DSL declaration.

    Directory placement and generic ``jobs``/``steps`` keys are deliberately
    insufficient.  Existing harness contracts identify themselves with
    harness-specific ids/contract keys; explicitly namespaced kinds are also
    accepted when paired with a schema/version field.
    """
    if str(data.get("orchestrator_id") or data.get("worker_id") or "").strip():
        # These identity keys belong to the harness extension vocabulary; an
        # ordinary CI/workflow document does not acquire them accidentally.
        return True
    strong_contract_fields = {
        "routing_rules",
        "route_selection_policy",
        "route_order",
        "intent_classification",
        "knowledge_bootstrap",
        "aggregation",
        "conflict_resolution",
        "worker_registry",
        "final_report_template",
        "output_contract",
        "quality_contract",
    }
    if strong_contract_fields.intersection(data):
        return True
    version = data.get("schema_version") or data.get("version")
    if version in (None, ""):
        return False
    kind = str(data.get("kind") or data.get("type") or "").strip().casefold()
    if kind in _EXECUTION_EXTENSION_KINDS:
        return True
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return False
    for namespace in _NAMESPACED_METADATA_EXTENSIONS:
        extension = metadata.get(namespace)
        if not isinstance(extension, dict):
            continue
        extension_version = (
            extension.get("schema_version")
            or extension.get("version")
            or version
        )
        extension_kind = str(
            extension.get("kind") or extension.get("type") or ""
        ).strip().casefold()
        if extension_version not in (None, "") and extension_kind in {
            "orchestrator", "worker", "execution_contract", "workflow_contract",
        }:
            return True
    return False


def _has_lexical_execution_extension_signal(
    skill_dir: Path,
    relative_path: str,
) -> bool:
    """Detect a top-level harness identity before parsing the full YAML graph.

    This check grants only *authority to validate*.  The authoritative compiler
    still has to parse and audit the entire document, so an oversized, cyclic,
    or malformed private DSL fails closed instead of being reclassified as an
    opaque CI file.  Generic ``jobs``/``steps`` documents do not match.
    """
    checked = validate_skill_resource(
        skill_dir,
        relative_path,
        expected_kind="file",
        require_relative=True,
    )
    if not checked.valid or checked.path is None:
        return False
    try:
        with checked.path.open("r", encoding="utf-8", errors="replace") as handle:
            prefix = handle.read(8_192)
    except OSError:
        return False
    # Auto-discovery is reserved for either an explicitly versioned,
    # namespaced extension or the two historical ChatDS identity keys. Generic
    # YAML keys such as ``output_contract`` or ``aggregation`` are common in
    # schemas, examples, and CI files and must never acquire blocking authority
    # merely by existing in a Skill package. Legacy documents without an
    # orchestrator/worker identity remain supported only when SKILL.md names
    # their exact path (the authority pass grants those before this probe).
    legacy_identity = re.search(
        r"^(?:orchestrator_id|worker_id)\s*:\s*[^#\s][^#\r\n]*$",
        prefix,
        re.MULTILINE,
    )
    versioned = re.search(
        r"^(?:schema_version|version)\s*:\s*[^#\s][^#\r\n]*$",
        prefix,
        re.MULTILINE | re.IGNORECASE,
    )
    namespaced_kind = re.search(
        r"^(?:kind|type)\s*:\s*(?:chatds|hermes|openclaw)\."
        r"(?:orchestrator|worker|execution_contract|workflow_contract)\s*$",
        prefix,
        re.MULTILINE | re.IGNORECASE,
    )
    namespaced_metadata = re.search(
        r"^metadata\s*:\s*(?:#.*)?(?:\r?\n)"
        r"(?:[ \t]+[^\r\n]*(?:\r?\n))*?"
        r"[ \t]+(?:chatds|hermes|openclaw)\s*:\s*(?:#.*)?$",
        prefix,
        re.MULTILINE | re.IGNORECASE,
    )
    return bool(
        legacy_identity
        or (versioned and (namespaced_kind or namespaced_metadata))
    )


def _namespaced_frontmatter_authority_resources(
    skill_dir: Path,
    known_resources: set[str],
    frontmatter: dict[str, Any],
) -> set[str]:
    """Read opt-in resource lists from versioned metadata extensions."""
    authorized: set[str] = set()
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return authorized
    for namespace in _NAMESPACED_METADATA_EXTENSIONS:
        namespace_value = metadata.get(namespace)
        if not isinstance(namespace_value, dict):
            continue
        for key in ("execution_contract", "workflow_contract", "resource_authority"):
            declaration = namespace_value.get(key)
            if not isinstance(declaration, dict):
                continue
            version = declaration.get("schema_version") or declaration.get("version")
            if version in (None, ""):
                continue
            for field in (
                "resource", "resources", "files", "orchestrators",
                "workers", "formats", "scripts",
            ):
                raw = declaration.get(field)
                values = raw if isinstance(raw, list) else [raw]
                for value in values:
                    resolved = _normalize_authority_resource_reference(
                        skill_dir,
                        known_resources,
                        value,
                    )
                    if resolved:
                        authorized.add(resolved)
    return authorized


def _discover_blocking_resource_authority(
    skill_dir: Path,
    linked_files: dict[str, list[str]],
    skill_body: str,
    frontmatter: dict[str, Any],
) -> tuple[set[str], dict[str, list[str]]]:
    """Compile the package resources allowed to create blocking Skill IR.

    All package files remain discoverable through ``linked_files`` and the
    resource graph.  A supporting file becomes execution-authoritative only
    when SKILL.md names it exactly, a versioned/namespaced harness extension
    identifies itself, or an already-authoritative structured declaration
    references it as another exact package resource.
    """
    known_resources = set(_flatten_linked_files(linked_files))
    authorized: set[str] = set()
    reasons: dict[str, list[str]] = {}

    def grant(path: str, reason: str, *, extension_override: bool = False) -> None:
        if path not in known_resources:
            return
        if not extension_override and not _resource_has_blocking_execution_role(path):
            return
        authorized.add(path)
        reasons.setdefault(path, [])
        if reason not in reasons[path]:
            reasons[path].append(reason)

    for relative_path in sorted(known_resources):
        if _skill_body_mentions_resource(skill_body, relative_path):
            grant(relative_path, "explicit_skill_reference")

    for relative_path in _namespaced_frontmatter_authority_resources(
        skill_dir,
        known_resources,
        frontmatter,
    ):
        grant(
            relative_path,
            "namespaced_frontmatter_extension",
            extension_override=True,
        )

    parsed_mappings: dict[str, dict[str, Any]] = {}
    for relative_path in sorted(known_resources):
        if PurePosixPath(relative_path).suffix.casefold() not in {".yaml", ".yml"}:
            continue
        if _has_lexical_execution_extension_signal(skill_dir, relative_path):
            grant(
                relative_path,
                "explicit_harness_extension_signal",
                extension_override=True,
            )
        if relative_path not in authorized:
            # Ordinary supporting YAML (for example a CI workflow with
            # jobs/steps) is opaque.  Do not even feed it to the private DSL
            # parser merely because it lives below workflows/.
            continue
        local_diagnostics: dict[str, list[dict[str, Any]]] = {
            "errors": [], "warnings": [], "info": [],
        }
        parsed = _load_yaml_mapping(
            skill_dir / relative_path,
            skill_dir,
            local_diagnostics,
            kind="resource_authority_probe",
        )
        if parsed is None:
            continue
        parsed_mappings[relative_path] = parsed
        if _is_explicit_execution_extension(parsed):
            grant(
                relative_path,
                "explicit_harness_extension",
                extension_override=True,
            )

    # Structured authority is transitive only through exact scalar resource
    # references.  This lets an authorized orchestrator own its workers and
    # formats without letting arbitrary prose or directory names own a DSL.
    pending = list(sorted(authorized))
    expanded: set[str] = set()
    while pending:
        source_file = pending.pop(0)
        if source_file in expanded:
            continue
        expanded.add(source_file)
        parsed = parsed_mappings.get(source_file)
        if not isinstance(parsed, dict):
            continue
        for value in _iter_bounded_scalar_strings(parsed):
            resolved = _normalize_authority_resource_reference(
                skill_dir,
                known_resources,
                value,
                source_file=source_file,
            )
            if not resolved or resolved in authorized:
                continue
            grant(resolved, f"declared_by:{source_file}")
            pending.append(resolved)
    return authorized, reasons


def _discover_workflow_contract(
    skill_dir: Path,
    linked_files: dict[str, list[str]],
    skill_body: str,
    frontmatter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files = _flatten_linked_files(linked_files)
    authorized_resources, authority_reasons = (
        _discover_blocking_resource_authority(
            skill_dir,
            linked_files,
            skill_body,
            frontmatter or {},
        )
    )
    workflow_files = [
        path for path in files
        if path in authorized_resources
        if path.startswith(("orchestration/", "workflows/", "workers/", "protocols/"))
    ]
    worker_files = [
        path for path in files
        if path in authorized_resources
        if "/workers/" in path or path.startswith("workers/") or _basename(path).startswith("worker-")
    ]
    format_files = [
        path for path in files
        if path in authorized_resources
        and (path.startswith("formats/") or "/formats/" in path)
    ]
    script_files = [
        path for path in files
        if path in authorized_resources
        and (
            path.startswith("scripts/")
            or PurePosixPath(path).suffix.casefold()
            in {".py", ".sh", ".bash", ".js", ".mjs", ".cjs"}
        )
    ]

    artifact_patterns: list[str] = []
    merge_requirements: list[str] = []
    sanity_checks: list[str] = []
    declared_external_sources: list[str] = []
    all_scanned_files = _dedupe(files)
    semantic_resource_files = [
        path for path in all_scanned_files
        if PurePosixPath(path).suffix.casefold()
        in _WORKFLOW_SEMANTIC_RESOURCE_SUFFIXES
    ]
    authoritative_semantic_files = [
        path for path in semantic_resource_files
        if path in authorized_resources
    ]
    # Supporting resources are opaque/advisory by default.  They remain in the
    # resource graph, but they never contribute blocking workflow/artifact IR
    # merely because of a conventional directory name or prose example.
    semantic_files = _dedupe(["SKILL.md", *authoritative_semantic_files])
    semantic_diagnostics: dict[str, list[dict[str, Any]]] = {
        "errors": [],
        "warnings": [],
        "info": [],
    }
    authoritative_scan_files = _dedupe([
        "SKILL.md", *authoritative_semantic_files,
    ])
    if len(authoritative_scan_files) > MAX_WORKFLOW_SEMANTIC_SCAN_FILES:
        _compiler_limit_error(
            semantic_diagnostics,
            code="workflow_semantic_file_limit_exceeded",
            message=(
                "The Skill contains more authoritative workflow text resources "
                "than can be scanned completely for execution semantics."
            ),
            field="workflow_contract.semantic_files",
            limit=MAX_WORKFLOW_SEMANTIC_SCAN_FILES,
            actual=len(authoritative_scan_files),
            source_file="SKILL.md",
        )
    scanned_files = semantic_files[:MAX_WORKFLOW_SEMANTIC_SCAN_FILES]
    semantic_total_chars = 0
    semantic_total_limit_exceeded = False
    semantic_fully_scanned_files = 0
    for rel_path in scanned_files:
        source_unreadable = False
        authoritative = True
        if rel_path == "SKILL.md":
            text = skill_body[:MAX_WORKFLOW_SEMANTIC_FILE_CHARS]
            source_chars = len(skill_body)
            source_truncated = source_chars > MAX_WORKFLOW_SEMANTIC_FILE_CHARS
        else:
            (
                text,
                source_chars,
                source_truncated,
                source_unreadable,
            ) = _read_semantic_text_resource(
                skill_dir / rel_path,
                skill_dir,
            )
            if source_unreadable:
                _diagnostic(
                    semantic_diagnostics,
                    "errors" if authoritative else "warnings",
                    "workflow_semantic_resource_unreadable",
                    (
                        "An authoritative Skill text resource could not be read "
                        "for semantic compilation."
                        if authoritative else
                        "A supporting/reference text resource could not be read; "
                        "it remains in the resource graph and was excluded from "
                        "contract inference."
                    ),
                    source_file=rel_path,
                )
        if source_truncated:
            if authoritative:
                _compiler_limit_error(
                    semantic_diagnostics,
                    code="workflow_semantic_file_size_exceeded",
                    message=(
                        "An authoritative Skill text resource is too large to "
                        "scan completely for workflow semantics."
                    ),
                    field="workflow_contract.semantic_files",
                    limit=MAX_WORKFLOW_SEMANTIC_FILE_CHARS,
                    actual=source_chars,
                    source_file=rel_path,
                )
            continue
        if source_unreadable:
            continue
        if semantic_total_chars + len(text) > MAX_WORKFLOW_SEMANTIC_TOTAL_CHARS:
            if authoritative and not semantic_total_limit_exceeded:
                semantic_total_limit_exceeded = True
                _compiler_limit_error(
                    semantic_diagnostics,
                    code="workflow_semantic_total_size_exceeded",
                    message=(
                        "The aggregate Skill text-resource closure is too large "
                        "to scan completely for workflow semantics."
                    ),
                    field="workflow_contract.semantic_files",
                    limit=MAX_WORKFLOW_SEMANTIC_TOTAL_CHARS,
                    actual=semantic_total_chars + len(text),
                    source_file=rel_path,
                )
                break
        semantic_total_chars += len(text)
        semantic_fully_scanned_files += 1
        if not text:
            continue
        artifact_patterns.extend(_extract_artifact_patterns(text))
        merge_requirements.extend(_extract_merge_requirements(text))
        sanity_checks.extend(_extract_sanity_checks(text))
        declared_external_sources.extend(_extract_external_sources(text))

    # Structured contract from the skill's OWN declarations (authoritative).
    output_contract: dict[str, Any] = {}
    output_contract.update(_parse_orchestrator_contract(
        skill_dir,
        authorized_resources=authorized_resources,
    ))
    for key, value in _parse_output_format_contract(
        skill_dir,
        authorized_resources=authorized_resources,
    ).items():
        output_contract.setdefault(key, value)
    output_format_files = [
        str(path) for path in output_contract.get("output_format_files") or []
        if isinstance(path, str)
    ]

    # Preserve the merge declaration's three states.  An explicit false means
    # a final artifact is produced semantically, not by byte concatenation.
    # Legacy contracts with no flag may still opt into merge behavior through
    # an explicit merge command; a final path alone is format-agnostic.
    merge_mandatory = output_contract.get("merge_mandatory")
    declares_merge = bool(
        merge_mandatory is True
        or (
            merge_mandatory is None
            and output_contract.get("merge_command")
        )
    )
    declares_modular = bool(output_contract.get("declared_modular_files"))
    execution_contract = _compile_execution_contract(
        skill_dir=skill_dir,
        worker_files=_dedupe(worker_files),
        output_contract=output_contract,
        frontmatter=frontmatter or {},
        skill_body=skill_body,
        authorized_resources=authorized_resources,
    )
    workers = [
        dict(worker)
        for worker in (execution_contract.get("workers") or [])
        if isinstance(worker, dict)
    ]
    worker_files = _dedupe(
        [
            str(worker.get("file"))
            for worker in workers
            if worker.get("file")
        ]
    )
    package_diagnostics = execution_contract.get("diagnostics") or {}
    if any(semantic_diagnostics[level] for level in ("errors", "warnings", "info")):
        if not isinstance(package_diagnostics, dict) or not package_diagnostics:
            package_diagnostics = {"errors": [], "warnings": [], "info": []}
        package_diagnostics.setdefault("errors", []).extend(
            semantic_diagnostics["errors"]
        )
        package_diagnostics.setdefault("warnings", []).extend(
            semantic_diagnostics["warnings"]
        )
        package_diagnostics.setdefault("info", []).extend(
            semantic_diagnostics["info"]
        )
        if execution_contract:
            execution_contract = dict(execution_contract)
            execution_contract["diagnostics"] = package_diagnostics
    if isinstance(package_diagnostics, dict) and package_diagnostics:
        summary_limits = {
            "scanned_files": (semantic_files, 80),
            "workflow_files": (workflow_files, 80),
            "worker_files": (worker_files, 80),
            "workers": (workers, 80),
            "format_files": (output_format_files, 40),
            "script_candidates": (_dedupe(script_files), 40),
            "artifact_patterns": (_dedupe(artifact_patterns), 80),
            "merge_requirements": (_dedupe(merge_requirements), 40),
            "declared_external_sources": (_dedupe(declared_external_sources), 40),
        }
        for field, (values, limit) in summary_limits.items():
            if len(values) > limit:
                _diagnostic(
                    package_diagnostics,
                    "warnings",
                    "workflow_summary_truncated",
                    "A workflow contract presentation field is a bounded sample; the complete safe resource closure remains available in linked_files.",
                    field=f"workflow_contract.{field}",
                    limit=limit,
                    actual=len(values),
                )
        if len(_dedupe(sanity_checks)) > 60:
            _diagnostic(
                package_diagnostics,
                "warnings",
                "workflow_summary_truncated",
                "The legacy prose-derived sanity-check manifest is presented as a bounded sample.",
                field="workflow_contract.sanity_checks",
                limit=60,
                actual=len(_dedupe(sanity_checks)),
            )
        _refresh_diagnostic_summary(package_diagnostics)
    normalized_output_contract = (
        execution_contract.get("output_contract")
        if isinstance(execution_contract.get("output_contract"), dict)
        else output_contract
    )

    contract: dict[str, Any] = {
        "resource_authority": (
            {
                "blocking_resources": sorted(authorized_resources),
                "advisory_resources": sorted(
                    set(files) - authorized_resources
                ),
                "reasons": {
                    path: authority_reasons[path]
                    for path in sorted(authority_reasons)
                },
                "policy": (
                    "Supporting resources are opaque/advisory unless SKILL.md "
                    "names them, a versioned namespaced extension declares them, "
                    "or an authoritative structured resource references them."
                ),
            }
            if authorized_resources else None
        ),
        "workflow_files": _dedupe(workflow_files)[:80],
        "orchestrator_files": [path for path in workflow_files if "orchestrator" in _basename(path).lower()][:20],
        "worker_files": _dedupe(worker_files)[:80],
        "workers": workers[:80],
        "format_files": output_format_files[:40],
        "script_candidates": _dedupe(script_files)[:40],
        "artifact_patterns": _dedupe(artifact_patterns)[:80],
        "merge_requirements": _dedupe(merge_requirements)[:40],
        "sanity_checks": _dedupe(sanity_checks)[:60],
        "sanity_checks_truncated": len(_dedupe(sanity_checks)) > 60,
        "sanity_checks_total": len(_dedupe(sanity_checks)),
        "declared_external_sources": _dedupe(declared_external_sources)[:40],
        "scanned_files_truncated": bool(
            len(semantic_files) > MAX_WORKFLOW_SEMANTIC_SCAN_FILES
            or semantic_total_limit_exceeded
            or any(
                item.get("code") in {
                    "workflow_semantic_file_size_exceeded",
                    "workflow_semantic_supporting_file_size_exceeded",
                    "workflow_semantic_supporting_total_size_exceeded",
                }
                for level in ("errors", "warnings")
                for item in semantic_diagnostics[level]
            )
        ),
        # Count only resources eligible for workflow-semantic compilation;
        # executable sources and bulk datasets remain in linked_files.
        "scanned_files_total": len(semantic_resource_files),
        "semantic_scanned_total_including_skill": semantic_fully_scanned_files,
        "semantic_scan_limit": MAX_WORKFLOW_SEMANTIC_SCAN_FILES,
        "output_contract": normalized_output_contract,
        "execution_contract": execution_contract,
        "package_diagnostics": package_diagnostics,
        "requires_worker_outputs": bool(workers),
        "requires_modular_artifacts": declares_modular or bool(artifact_patterns),
        "requires_merge": declares_merge,
        "recommended_execution": [
            "Load __manifest__, then inspect orchestrator/workflow files first.",
            "Inspect each declared worker file and collect evidence for every worker before final synthesis.",
            "Generate the declared modular artifacts/checklist in the session workspace when artifact patterns are specified.",
            "Use run_skill_script for declared Python, Shell, or JavaScript entrypoints; reserve run_skill_python for an explicitly declared public Python function call, and use execute_code only for small ad-hoc calculations.",
            "Run the declared merge/sanity steps and verify the final artifact before stopping.",
        ],
    }
    return {key: value for key, value in contract.items() if value not in ([], {}, None, False)}


def _flatten_linked_files(linked_files: dict[str, list[str]]) -> list[str]:
    result: list[str] = []
    for files in linked_files.values():
        result.extend(str(path) for path in files if isinstance(path, str))
    return _dedupe(result)


def _read_text_resource(path: Path, skill_root: Path, limit: int = 120_000) -> str:
    try:
        checked = validate_skill_resource(skill_root, path, expected_kind="file")
        if (
            not checked.valid
            or checked.path is None
            or checked.path.suffix.lower() not in _TEXT_RESOURCE_SUFFIXES
        ):
            return ""
        return checked.path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _read_semantic_text_resource(
    path: Path,
    skill_root: Path,
) -> tuple[str, int, bool, bool]:
    """Read one complete bounded text resource for semantic discovery.

    ``_read_text_resource`` is intentionally a presentation helper and may
    return a prefix.  The compiler path must instead report whether any byte
    was omitted so the resulting execution contract can fail closed.
    """
    try:
        checked = validate_skill_resource(
            skill_root,
            path,
            expected_kind="file",
        )
        if (
            not checked.valid
            or checked.path is None
            or checked.path.suffix.casefold() not in _TEXT_RESOURCE_SUFFIXES
        ):
            return "", 0, False, True
        with checked.path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_WORKFLOW_SEMANTIC_FILE_CHARS + 1)
        if len(text) > MAX_WORKFLOW_SEMANTIC_FILE_CHARS:
            return (
                text[:MAX_WORKFLOW_SEMANTIC_FILE_CHARS],
                len(text),
                True,
                False,
            )
        return text, len(text), False, False
    except OSError:
        return "", 0, False, True


def _basename(path: str) -> str:
    return str(path).rsplit("/", 1)[-1]


def _worker_id_from_path(path: str) -> str:
    name = _basename(path)
    stem = name.rsplit(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-") or stem


def _worker_role_hint(path: Path, skill_root: Path) -> str:
    text = _read_text_resource(path, skill_root, limit=8_000)
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
    """Extract only prose that explicitly declares generated deliverables.

    Resource text commonly mentions input files, examples, templates, and
    reference documents.  A filename convention (``report.md``), wildcard, or
    numbered Markdown module is therefore not sufficient evidence that the
    Skill requires an artifact workflow.  Structured output contracts remain
    authoritative; this legacy fallback requires an output label, a creation
    action, or an equivalent predicate local to the filename.
    """
    patterns: list[str] = []
    filename_re = re.compile(
        r"(?<![\w.-])(?:`)?("
        r"(?:[A-Za-z0-9_{}*?.-]+/)*[A-Za-z0-9_{}*?.-]+\."
        r"(?:md|markdown|rst|json|jsonl|ndjson|csv|tsv|txt|yaml|yml|html|htm|"
        r"xml|svg|pdf|docx|xlsx|pptx|zip|parquet|png|jpe?g|gif|webp)"
        r")(?:`)?(?!(?:[\w-]|\.[A-Za-z0-9]))",
        re.IGNORECASE,
    )
    output_label_re = re.compile(
        r"(?:\b(?:final\s+output|output\s+files?|deliverables?|artifacts?|"
        r"generated\s+files?|result\s+files?)\b|"
        r"(?:最终输出|输出文件|交付物|产物|生成文件|结果文件))\s*"
        r"(?::|：|=|\||-)?",
        re.IGNORECASE,
    )
    output_action_re = re.compile(
        r"(?:\b(?:create|generate|write|save|export|produce|emit|persist|store|"
        r"deliver|render)\b|(?:生成|创建|新建|写入|保存|导出|产出|交付|渲染))",
        re.IGNORECASE,
    )
    negation_re = re.compile(
        r"(?:\b(?:do\s+not|don't|dont|never|without|skip|avoid)\b|"
        r"(?:不要|无需|不用|禁止|跳过|避免))",
        re.IGNORECASE,
    )
    reference_re = re.compile(
        r"(?:\b(?:from|using|according\s+to|based\s+on|refer\s+to|see|read)\b|"
        r"(?:来自|使用|按照|依据|基于|参见|参考|读取))",
        re.IGNORECASE,
    )
    direct_target_re = re.compile(
        r"(?:\b(?:to|into|as|named)\s+|(?:保存到|写入|导出到|输出为|命名为)\s*)$",
        re.IGNORECASE,
    )
    output_predicate_re = re.compile(
        r"^.{0,64}(?:\b(?:is|are|will\s+be|must\s+be|should\s+be)\s+"
        r"(?:the\s+)?(?:final\s+)?(?:output|deliverable|artifact|generated\s+file|"
        r"result\s+file)\b|(?:作为|是|将作为).{0,20}(?:最终输出|输出文件|交付物|产物))",
        re.IGNORECASE,
    )
    input_label_re = re.compile(
        r"(?:\b(?:input|source|reference|template)\s+files?\b|"
        r"(?:输入|源|参考|模板)文件)\s*(?::|：|=|\||-)?",
        re.IGNORECASE,
    )

    output_list_open = False
    fence: tuple[str, int] | None = None
    for line in str(text or "").splitlines():
        fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            output_list_open = False
            continue
        if fence is not None:
            continue

        stripped = line.strip()
        if not stripped:
            output_list_open = False
            continue
        line_has_output_label = bool(output_label_re.search(line))
        if input_label_re.search(line):
            output_list_open = False
        if stripped.startswith("#") and not line_has_output_label:
            output_list_open = False
        list_item = bool(re.match(
            r"^\s*(?:[-*+]\s+|\d+[.)]\s+|\|\s*|`?[A-Za-z0-9_{}*?.-]+(?:/|\.))",
            line,
        ))
        inherited_output_list = output_list_open and list_item
        if output_list_open and not list_item and not line_has_output_label:
            # A normal prose line begins a new semantic paragraph even when
            # Markdown omitted a blank separator.
            output_list_open = False

        for match in filename_re.finditer(line):
            prefix = line[max(0, match.start() - 180):match.start()]
            suffix = line[match.end():match.end() + 80]
            declared = inherited_output_list

            labels = list(output_label_re.finditer(prefix))
            if labels:
                label = labels[-1]
                after_label = prefix[label.end():]
                # A later sentence/clause can change the role of the file.
                declared = not re.search(r"[.;。；]", after_label)

            actions = list(output_action_re.finditer(prefix))
            if not declared and actions:
                action = actions[-1]
                before_action = prefix[max(0, action.start() - 24):action.start()]
                after_action = prefix[action.end():]
                negated = bool(negation_re.search(before_action))
                is_reference = bool(reference_re.search(after_action))
                direct_target = bool(direct_target_re.search(after_action[-32:]))
                declared = not negated and (not is_reference or direct_target)

            if not declared and output_predicate_re.search(suffix):
                declared = True
            if declared:
                patterns.append(match.group(1))

        file_count_re = re.compile(
            r"\b(\d+)\s+(?:modular\s+|content\s+|output\s+|generated\s+)?files?\b|"
            r"(\d+)\s*(?:个|份)(?:模块化)?(?:输出|生成|交付)?文件",
            re.IGNORECASE,
        )
        for file_count in file_count_re.finditer(line):
            prefix = line[:file_count.start()]
            local = line[max(0, file_count.start() - 160):file_count.end() + 40]
            actions = list(output_action_re.finditer(prefix))
            labels = list(output_label_re.finditer(prefix))
            explicit_output_noun = bool(re.search(
                r"(?:\b(?:output|generated|deliverable|artifact)\s+files?\b|"
                r"(?:输出|生成|交付)文件)",
                local,
                re.IGNORECASE,
            ))
            declared_count = explicit_output_noun or bool(labels)
            if actions:
                action = actions[-1]
                before_action = prefix[max(0, action.start() - 24):action.start()]
                declared_count = declared_count or not negation_re.search(before_action)
            if declared_count:
                count = file_count.group(1) or file_count.group(2)
                patterns.append(f"declared_file_count:{count}")
        if line_has_output_label and not input_label_re.search(line):
            output_list_open = True
    return _dedupe(patterns)


def _extract_merge_requirements(text: str) -> list[str]:
    requirements: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered_line = stripped.lower()
        action = re.search(
            r"\b(?:auto[- ]?merge|merge|concatenate|combine|cat)\b|"
            r"(?:自动合并|合并|拼接)",
            stripped,
            re.IGNORECASE,
        )
        if action is None:
            continue
        before_action = stripped[max(0, action.start() - 48):action.start()]
        negated = re.search(
            r"(?:\b(?:do\s+not|don't|must\s+not|never|without|no\s+need\s+to)\b|"
            r"(?:不要|不得|无需|不应|禁止))[^.!?。！？；;]{0,36}$",
            before_action,
            re.IGNORECASE,
        )
        if negated:
            continue
        explicit_merge = bool(
            "auto-merge" in lowered_line
            or "auto merge" in lowered_line
            or ("cat " in lowered_line and ">" in lowered_line)
            or (
                re.search(r"\b(?:merge|concatenate|combine)\b", lowered_line)
                and re.search(r"\b(?:report|files?|artifacts?|outputs?)\b", lowered_line)
            )
            or re.search(r"(?:自动合并|合并|拼接).{0,32}(?:报告|文件|产物|输出)", stripped)
        )
        if explicit_merge:
            requirements.append(stripped[:240])
    return _dedupe(requirements)


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
    """Extract bounded, explicitly declared source labels or HTTPS hosts.

    Source discovery is presentation metadata, not a domain ontology.  Only a
    labelled ``Sources:``/``数据源:`` declaration, an HTTPS Markdown link, or
    a literal HTTPS URL is considered.  Merely mentioning a well-known
    organization/database never creates a machine-readable source claim.
    """
    sample = str(text or "")[:MAX_EXTERNAL_SOURCE_SCAN_CHARS]
    candidates: list[tuple[int, str]] = []
    markdown_spans: list[tuple[int, int]] = []

    def safe_label(value: str) -> str | None:
        normalized = re.sub(r"\s+", " ", str(value or "")).strip().strip("`*_ ")
        if (
            not normalized
            or len(normalized) > MAX_EXTERNAL_SOURCE_LABEL_CHARS
            or any(ord(char) < 32 for char in normalized)
        ):
            return None
        return normalized

    def https_host(url: str) -> str | None:
        if len(url) > MAX_EXTERNAL_SOURCE_URL_CHARS:
            return None
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
        except (TypeError, ValueError):
            return None
        if parsed.scheme.casefold() != "https" or not host:
            return None
        normalized = host.rstrip(".").casefold()
        if (
            not normalized
            or len(normalized) > 253
            or re.fullmatch(r"[a-z0-9.-]+", normalized) is None
        ):
            return None
        return normalized

    markdown_link_re = re.compile(
        rf"(?<!!)\[([^\]\n]{{1,{MAX_EXTERNAL_SOURCE_LABEL_CHARS}}})\]"
        rf"\((https://[^\s<>()\]]{{1,{MAX_EXTERNAL_SOURCE_URL_CHARS}}})\)",
        re.IGNORECASE,
    )
    for match in markdown_link_re.finditer(sample):
        host = https_host(match.group(2).rstrip(".,;:!?，。；：！？"))
        label = safe_label(match.group(1))
        if host and label:
            candidates.append((match.start(), label))
            markdown_spans.append(match.span())

    source_line_re = re.compile(
        r"^\s*(?:[-*+]\s*)?(?:sources?|data\s+sources?|数据源)\s*[:：]\s*(.*?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in source_line_re.finditer(sample):
        declaration = match.group(1)
        for part_match in re.finditer(r"[^,，;；|]+", declaration):
            part = part_match.group(0).strip()
            if "](" in part or part.casefold().startswith("https://"):
                continue
            label = safe_label(part)
            if label:
                candidates.append((match.start(1) + part_match.start(), label))

    bare_url_re = re.compile(
        rf"https://[^\s<>()\[\]{{}}\"']{{1,{MAX_EXTERNAL_SOURCE_URL_CHARS}}}",
        re.IGNORECASE,
    )
    for match in bare_url_re.finditer(sample):
        if any(start <= match.start() < end for start, end in markdown_spans):
            continue
        url = match.group(0).rstrip(".,;:!?，。；：！？")
        host = https_host(url)
        if host:
            candidates.append((match.start(), host))

    ordered = [label for _offset, label in sorted(candidates, key=lambda item: item[0])]
    return _dedupe(ordered)[:MAX_EXTERNAL_SOURCE_COUNT]


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
