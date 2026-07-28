"""Bounded symbolic compiler for declarative worker knowledge gates.

This module deliberately stops before runtime capability resolution.  It turns
portable Skill declarations into a finite, canonical selector graph while
leaving native-tool aliases, Skill entrypoints, HTTP grants, MCP descriptors,
and exact candidate IDs to the run-frozen capability compiler.

The legacy ``tools: [a, b]`` spelling is ambiguous: some packages mean
cross-source verification while others mean fallback alternatives.  The only
lossless compatibility interpretation available without mining prose is one
conditional ``one_of`` group.  Packages that require AND-of-OR semantics can
declare ``tool_groups`` or ``tools: {all_of: ...}`` explicitly.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

KNOWLEDGE_GATE_SCHEMA_VERSION = 1
MAX_GATE_CHECKS = 128
MAX_GATE_IDENTIFIER_CHARS = 160
MAX_GATE_TEXT_CHARS = 8_000
MAX_GATE_GROUPS_PER_BRANCH = 64
MAX_GATE_TOTAL_GROUPS = 256
MAX_GATE_SELECTORS_PER_GROUP = 64
MAX_GATE_TOTAL_SELECTORS = 1_024
MAX_GATE_SELECTOR_CHARS = 512
MAX_GATE_LOCAL_RESOURCES = 512
MAX_GATE_GLOB_SCAN_ENTRIES = 20_000
MAX_GATE_STRUCTURE_DEPTH = 24
MAX_GATE_STRUCTURE_NODES = 8_000
MAX_GATE_DIAGNOSTICS = 512

_BRANCH_KEYS = {
    "if_yes": "yes",
    "if_no": "no",
    "if_unknown": "unknown",
}
_GATE_KEYS = frozenset({
    "checks",
    "description",
    "metadata",
    "mid_execution_search_policy",
    "version",
})
_CHECK_KEYS = frozenset({
    "description",
    "id",
    "name",
    "question",
    "tools",
    "tool_groups",
    *_BRANCH_KEYS,
})
_BRANCH_MAPPING_KEYS = frozenset({
    "action",
    "description",
    "instruction",
    "tools",
    "tool_groups",
})
_TOOL_EXPRESSION_KEYS = frozenset({"any_of", "all_of"})
_TOOL_GROUP_KEYS = frozenset({
    "any_of",
    "all_of",
    "description",
    "id",
    "mode",
    "selectors",
    "tools",
})
_SELECTOR_DESCRIPTOR_KEYS = frozenset({
    "description",
    "file",
    "name",
    "optional",
    "path",
    "required",
    "resource",
    "selector",
    "skill",
    "source",
    "tool",
    "tool_selector",
})
_SKILL_NAME_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?$",
    re.IGNORECASE,
)
_NATIVE_SELECTOR_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_.-]*(?:\([^\r\n\x00]{0,256}\))?$"
)
_GATE_IDENTIFIER_SEPARATORS = frozenset("._:/-")
_GLOB_MAGIC_RE = re.compile(r"[*?\[]")
_LOCAL_RESOURCE_SUFFIXES = frozenset({
    ".bash",
    ".cfg",
    ".csv",
    ".db",
    ".docx",
    ".html",
    ".ipynb",
    ".ini",
    ".js",
    ".jl",
    ".jpeg",
    ".jpg",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".parquet",
    ".pdf",
    ".png",
    ".py",
    ".r",
    ".rst",
    ".sh",
    ".sql",
    ".sqlite",
    ".svg",
    ".toml",
    ".tsv",
    ".tsx",
    ".txt",
    ".xlsx",
    ".xml",
    ".yaml",
    ".yml",
})
_LOCAL_SOURCE_NAMES = frozenset({"local", "package", "project"})


def is_canonical_knowledge_gate_identifier(value: Any) -> bool:
    """Return whether ``value`` is one portable knowledge-gate identifier.

    IDs are intentionally Unicode-aware so internationalized Skill packages do
    not need lossy transliteration.  Requiring NFC and Unicode letter/number/
    mark categories keeps equivalent spellings deterministic while excluding
    whitespace, controls, format characters, symbols, and prompt delimiters.
    The small ASCII separator set covers generated and hierarchical IDs.
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_GATE_IDENTIFIER_CHARS
        or unicodedata.normalize("NFC", value) != value
    ):
        return False

    first = value[0]
    if first != "_" and unicodedata.category(first)[0] not in {"L", "N"}:
        return False
    return all(
        character in _GATE_IDENTIFIER_SEPARATORS
        or unicodedata.category(character)[0] in {"L", "M", "N"}
        for character in value[1:]
    )


def _validate_skill_root(path: Path):
    # Keep this compiler importable while ``skills.__init__`` is still loading:
    # that package imports its loader, and the loader imports this module.
    from skills.path_safety import validate_skill_root

    return validate_skill_root(path)


def _validate_skill_resource(
    skill_dir: Path,
    resource: str,
    *,
    expected_kind: str,
    require_relative: bool,
):
    from skills.path_safety import validate_skill_resource

    return validate_skill_resource(
        skill_dir,
        resource,
        expected_kind=expected_kind,
        require_relative=require_relative,
    )


@dataclass(frozen=True)
class KnowledgeGateDiagnostic:
    """One stable loader-facing compiler diagnostic."""

    level: str
    code: str
    message: str
    context: dict[str, Any]


@dataclass(frozen=True)
class SymbolicKnowledgeGateCompilation:
    """Finite symbolic output and its loader closure."""

    ir: dict[str, Any]
    skill_refs: tuple[str, ...]
    local_resources: tuple[str, ...]
    diagnostics: tuple[KnowledgeGateDiagnostic, ...]


class _Compiler:
    def __init__(
        self,
        *,
        skill_dir: Path,
        source_file: str | None,
        worker_id: str,
    ) -> None:
        root_check = _validate_skill_root(skill_dir)
        self.skill_dir = (
            root_check.path
            if root_check.valid and root_check.path is not None
            else Path(skill_dir)
        )
        self.source_file = source_file
        self.worker_id = str(worker_id or "").strip()
        self.diagnostics: list[KnowledgeGateDiagnostic] = []
        self.skill_refs: list[str] = []
        self.local_resources: list[str] = []
        self.resource_expansions: list[dict[str, Any]] = []
        self.total_selectors = 0
        self.selector_limit_failed = False
        self.total_groups = 0
        self.group_limit_failed = False
        self.glob_scan_entries = 0
        self.glob_scan_failed = False
        self.local_resource_limit_failed = False
        self.structure_nodes = 0
        self.structure_failed = False
        self.diagnostic_limit_failed = False
        if not is_canonical_knowledge_gate_identifier(self.worker_id):
            self.error(
                "knowledge_gate_worker_id_invalid",
                "A knowledge-gate worker id must be one canonical bounded "
                "Unicode identifier.",
                field=f"workers[{self.worker_id}].knowledge_gate",
            )
        if not root_check.valid:
            self.error(
                "knowledge_gate_skill_root_invalid",
                "The worker knowledge gate cannot be compiled against an invalid Skill root.",
                field=f"workers[{worker_id}].knowledge_gate",
                reason=root_check.code,
            )

    def issue(
        self,
        level: str,
        code: str,
        message: str,
        **context: Any,
    ) -> None:
        if len(self.diagnostics) >= MAX_GATE_DIAGNOSTICS:
            if not self.diagnostic_limit_failed:
                self.diagnostic_limit_failed = True
                self.diagnostics.append(
                    KnowledgeGateDiagnostic(
                        "errors",
                        "knowledge_gate_diagnostic_limit_exceeded",
                        "The knowledge-gate compiler exhausted its bounded diagnostic budget.",
                        {
                            key: value
                            for key, value in {
                                "source_file": self.source_file,
                                "worker_id": self.worker_id,
                                "limit": MAX_GATE_DIAGNOSTICS,
                            }.items()
                            if value not in (None, "")
                        },
                    )
                )
            return
        normalized = {
            key: value
            for key, value in {
                "source_file": self.source_file,
                "worker_id": self.worker_id,
                **context,
            }.items()
            if value not in (None, "", [], {})
        }
        self.diagnostics.append(
            KnowledgeGateDiagnostic(level, code, message, normalized)
        )

    def error(self, code: str, message: str, **context: Any) -> None:
        self.issue("errors", code, message, **context)

    def warning(self, code: str, message: str, **context: Any) -> None:
        self.issue("warnings", code, message, **context)

    def _bounded_graph(self, value: Any, *, field: str) -> bool:
        """Reject recursive or oversized nested tool expressions."""

        stack: list[tuple[Any, int, str, bool]] = [(value, 0, field, False)]
        active: set[int] = set()
        visited: set[int] = set()
        while stack:
            node, depth, path, exiting = stack.pop()
            is_container = isinstance(node, (dict, list, tuple, set))
            if exiting:
                if is_container:
                    active.discard(id(node))
                continue
            self.structure_nodes += 1
            if self.structure_nodes > MAX_GATE_STRUCTURE_NODES:
                if not self.structure_failed:
                    self.structure_failed = True
                    self.error(
                        "knowledge_gate_structure_limit_exceeded",
                        "The knowledge-gate declaration exceeds the bounded structure-node limit.",
                        field=field,
                        yaml_path=path,
                        limit=MAX_GATE_STRUCTURE_NODES,
                        actual=self.structure_nodes,
                    )
                return False
            if depth > MAX_GATE_STRUCTURE_DEPTH:
                self.error(
                    "knowledge_gate_structure_depth_exceeded",
                    "The knowledge-gate declaration exceeds the bounded nesting-depth limit.",
                    field=field,
                    yaml_path=path,
                    limit=MAX_GATE_STRUCTURE_DEPTH,
                    actual=depth,
                )
                return False
            if not is_container:
                continue
            identity = id(node)
            if identity in active:
                self.error(
                    "knowledge_gate_structure_cycle",
                    "The knowledge-gate declaration contains a recursive alias cycle.",
                    field=field,
                    yaml_path=path,
                )
                return False
            if identity in visited:
                continue
            visited.add(identity)
            active.add(identity)
            stack.append((node, depth, path, True))
            direct_children = len(node)
            if (
                self.structure_nodes + direct_children
                > MAX_GATE_STRUCTURE_NODES
            ):
                if not self.structure_failed:
                    self.structure_failed = True
                    self.error(
                        "knowledge_gate_structure_limit_exceeded",
                        "The knowledge-gate declaration exceeds the bounded structure-node limit.",
                        field=field,
                        yaml_path=path,
                        limit=MAX_GATE_STRUCTURE_NODES,
                        actual=self.structure_nodes + direct_children,
                    )
                return False
            if isinstance(node, dict):
                children = list(node.items())
                for key, child in reversed(children):
                    stack.append((child, depth + 1, f"{path}.{key}", False))
            else:
                children = list(node)
                if isinstance(node, set):
                    children.sort(key=str)
                for index, child in reversed(list(enumerate(children))):
                    stack.append((child, depth + 1, f"{path}[{index}]", False))
        return True

    def _bounded_text(
        self,
        value: Any,
        *,
        field: str,
        required: bool = False,
    ) -> str:
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value.strip()
        else:
            self.error(
                "knowledge_gate_text_type_invalid",
                "Knowledge-gate questions and branch actions must be strings.",
                field=field,
                value_type=type(value).__name__,
            )
            text = ""
        if len(text) > MAX_GATE_TEXT_CHARS:
            self.error(
                "knowledge_gate_text_limit_exceeded",
                "A knowledge-gate text field exceeds the bounded character limit.",
                field=field,
                limit=MAX_GATE_TEXT_CHARS,
                actual=len(text),
            )
            text = text[:MAX_GATE_TEXT_CHARS]
        if required and not text:
            self.error(
                "knowledge_gate_text_missing",
                "A knowledge-gate check requires a non-empty question.",
                field=field,
            )
        return text

    def _canonical_check_id(self, value: Any, *, field: str) -> str:
        text = str(value or "").strip()
        if not is_canonical_knowledge_gate_identifier(text):
            self.error(
                "knowledge_gate_check_id_invalid",
                "A knowledge-gate check id must be one canonical bounded "
                "Unicode identifier.",
                field=field,
            )
            return ""
        return text

    def _looks_like_local_resource(self, value: str) -> bool:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        return bool(
            normalized.startswith(("./", "../", "/"))
            or "/" in normalized
            or path.suffix.casefold() in _LOCAL_RESOURCE_SUFFIXES
        )

    def _canonical_local_pattern(
        self,
        value: str,
        *,
        field: str,
    ) -> str:
        if (
            not value
            or value != value.strip()
            or "\\" in value
            or "\x00" in value
            or any(ord(char) < 32 for char in value)
        ):
            self.error(
                "knowledge_gate_local_path_invalid",
                "A knowledge-gate local resource must be a safe package-relative POSIX path.",
                field=field,
                resource=value,
            )
            return ""
        normalized = value.removeprefix("./")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            self.error(
                "knowledge_gate_local_path_invalid",
                "A knowledge-gate local resource must remain inside the Skill package.",
                field=field,
                resource=value,
            )
            return ""
        for component in path.parts:
            if component != "**" and "**" in component:
                self.error(
                    "knowledge_gate_glob_invalid",
                    "Recursive glob syntax is supported only as a complete '**' path component.",
                    field=field,
                    resource=value,
                )
                return ""
            if "[" in component and "]" not in component:
                self.error(
                    "knowledge_gate_glob_invalid",
                    "A knowledge-gate package glob contains an unterminated character class.",
                    field=field,
                    resource=value,
                )
                return ""
        if len(normalized) > MAX_GATE_SELECTOR_CHARS:
            self.error(
                "knowledge_gate_selector_limit_exceeded",
                "A knowledge-gate selector exceeds the bounded character limit.",
                field=field,
                limit=MAX_GATE_SELECTOR_CHARS,
                actual=len(normalized),
            )
            return ""
        return str(path)

    def _directory_entries(
        self,
        directory: Path,
        *,
        field: str,
        selector: str,
        scan_state: list[int],
    ) -> list[os.DirEntry[str]]:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            self.error(
                "knowledge_gate_glob_unreadable",
                "A knowledge-gate package glob traverses an unreadable directory.",
                field=field,
                selector=selector,
                failure_type=type(exc).__name__,
            )
            return []
        scan_state[0] += len(entries)
        self.glob_scan_entries += len(entries)
        if (
            scan_state[0] > MAX_GATE_GLOB_SCAN_ENTRIES
            or self.glob_scan_entries > MAX_GATE_GLOB_SCAN_ENTRIES
        ):
            self.glob_scan_failed = True
            self.error(
                "knowledge_gate_glob_scan_limit_exceeded",
                "A knowledge-gate package glob exceeds the bounded directory-scan limit.",
                field=field,
                selector=selector,
                limit=MAX_GATE_GLOB_SCAN_ENTRIES,
                actual=max(scan_state[0], self.glob_scan_entries),
            )
            return []
        return entries

    def _expand_local_glob(self, pattern: str, *, field: str) -> list[str]:
        """Expand a package glob without following symlinks."""

        parts = PurePosixPath(pattern).parts
        pending: list[tuple[Path, int]] = [(self.skill_dir, 0)]
        visited_states: set[tuple[str, int]] = set()
        matches: list[str] = []
        scan_state = [0]
        if self.glob_scan_failed:
            self.error(
                "knowledge_gate_glob_scan_budget_exhausted",
                "The worker knowledge gate has exhausted its package-glob scan budget.",
                field=field,
                selector=pattern,
                limit=MAX_GATE_GLOB_SCAN_ENTRIES,
            )
            return []
        while pending:
            current, index = pending.pop()
            state = (str(current), index)
            if state in visited_states:
                continue
            visited_states.add(state)
            if (
                scan_state[0] > MAX_GATE_GLOB_SCAN_ENTRIES
                or self.glob_scan_failed
            ):
                break
            if index >= len(parts):
                try:
                    mode = os.lstat(current).st_mode
                except OSError:
                    continue
                if stat.S_ISREG(mode) and not stat.S_ISLNK(mode):
                    try:
                        relative = str(current.relative_to(self.skill_dir))
                    except ValueError:
                        continue
                    checked = _validate_skill_resource(
                        self.skill_dir,
                        relative,
                        expected_kind="file",
                        require_relative=True,
                    )
                    if checked.valid and checked.path is not None:
                        matches.append(str(checked.path.relative_to(self.skill_dir)))
                continue

            component = parts[index]
            if component == "**":
                pending.append((current, index + 1))
                for entry in reversed(
                    self._directory_entries(
                        current,
                        field=field,
                        selector=pattern,
                        scan_state=scan_state,
                    )
                ):
                    try:
                        if entry.is_symlink():
                            continue
                        if (
                            index + 1 == len(parts)
                            and entry.is_file(follow_symlinks=False)
                        ):
                            pending.append((Path(entry.path), index + 1))
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    pending.append((Path(entry.path), index))
                continue

            for entry in reversed(
                self._directory_entries(
                    current,
                    field=field,
                    selector=pattern,
                    scan_state=scan_state,
                )
            ):
                if not fnmatch.fnmatchcase(entry.name, component):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if index + 1 < len(parts):
                        if entry.is_dir(follow_symlinks=False):
                            pending.append((Path(entry.path), index + 1))
                    elif entry.is_file(follow_symlinks=False):
                        pending.append((Path(entry.path), index + 1))
                except OSError:
                    continue
        if self.glob_scan_failed:
            return []
        matches = list(dict.fromkeys(sorted(matches)))
        if len(matches) > MAX_GATE_LOCAL_RESOURCES:
            self.error(
                "knowledge_gate_glob_match_limit_exceeded",
                "A knowledge-gate package glob expands to too many regular files.",
                field=field,
                selector=pattern,
                limit=MAX_GATE_LOCAL_RESOURCES,
                actual=len(matches),
            )
            matches = matches[:MAX_GATE_LOCAL_RESOURCES]
        if not matches:
            self.error(
                "knowledge_gate_glob_no_matches",
                "A declared knowledge-gate package glob matches no safe regular files.",
                field=field,
                selector=pattern,
            )
        return matches

    def _record_local_selector(self, selector: str, *, field: str) -> str:
        canonical = self._canonical_local_pattern(selector, field=field)
        if not canonical:
            return ""
        if len(self.local_resources) >= MAX_GATE_LOCAL_RESOURCES:
            if not self.local_resource_limit_failed:
                self.local_resource_limit_failed = True
                self.error(
                    "knowledge_gate_local_resource_limit_exceeded",
                    "A worker knowledge gate resolves to too many local package resources.",
                    field=field,
                    limit=MAX_GATE_LOCAL_RESOURCES,
                    actual=len(self.local_resources) + 1,
                )
            return canonical
        if _GLOB_MAGIC_RE.search(canonical) or "**" in PurePosixPath(canonical).parts:
            matches = self._expand_local_glob(canonical, field=field)
            remaining = max(
                0,
                MAX_GATE_LOCAL_RESOURCES - len(self.local_resources),
            )
            if len(matches) > remaining:
                if not self.local_resource_limit_failed:
                    self.local_resource_limit_failed = True
                    self.error(
                        "knowledge_gate_local_resource_limit_exceeded",
                        "A worker knowledge gate resolves to too many local package resources.",
                        field=field,
                        limit=MAX_GATE_LOCAL_RESOURCES,
                        actual=len(self.local_resources) + len(matches),
                    )
                matches = matches[:remaining]
            self.resource_expansions.append({
                "selector": canonical,
                "resources": matches,
            })
            self.local_resources.extend(matches)
            return canonical
        checked = _validate_skill_resource(
            self.skill_dir,
            canonical,
            expected_kind="file",
            require_relative=True,
        )
        if not checked.valid or checked.path is None:
            code = (
                "knowledge_gate_local_resource_missing"
                if checked.code == "missing_resource"
                else "knowledge_gate_local_resource_unsafe"
            )
            self.error(
                code,
                "A knowledge-gate selector references an unavailable or unsafe package resource.",
                field=field,
                resource=canonical,
                reason=checked.code,
            )
            return ""
        relative = str(checked.path.relative_to(self.skill_dir))
        self.local_resources.append(relative)
        self.resource_expansions.append({
            "selector": relative,
            "resources": [relative],
        })
        return relative

    def _canonical_selector(self, value: Any, *, field: str) -> str:
        if not isinstance(value, str):
            self.error(
                "knowledge_gate_selector_type_invalid",
                "Every knowledge-gate selector must be a string or one supported descriptor.",
                field=field,
                value_type=type(value).__name__,
            )
            return ""
        selector = value.strip()
        if not selector or len(selector) > MAX_GATE_SELECTOR_CHARS:
            self.error(
                "knowledge_gate_selector_limit_exceeded",
                "A knowledge-gate selector must be non-empty and within the "
                "bounded character limit.",
                field=field,
                limit=MAX_GATE_SELECTOR_CHARS,
                actual=len(selector),
            )
            return ""
        if selector.casefold().startswith("skill:"):
            skill_name = selector.split(":", 1)[1].strip()
            if not _SKILL_NAME_RE.fullmatch(skill_name):
                self.error(
                    "knowledge_gate_skill_ref_invalid",
                    "A knowledge-gate Skill selector contains an invalid Skill name.",
                    field=field,
                    selector=selector,
                )
                return ""
            canonical = f"skill:{skill_name}"
            self.skill_refs.append(skill_name)
            return canonical
        if "(" in selector:
            return self._canonical_narrowed_selector(
                selector,
                field=field,
            )
        if self._looks_like_local_resource(selector):
            return self._record_local_selector(selector, field=field)
        if not _NATIVE_SELECTOR_RE.fullmatch(selector):
            self.error(
                "knowledge_gate_native_selector_invalid",
                "A knowledge-gate native selector must use one bounded exact selector spelling.",
                field=field,
                selector=selector,
            )
            return ""
        return selector

    def _canonical_narrowed_selector(
        self,
        selector: str,
        *,
        field: str,
    ) -> str:
        """Accept only the existing no-shell exact command grammar.

        Parenthesized selectors are not generic aliases: silently lowering
        ``WebSearch(policy)`` or an unsafe Bash expression to its bare bridge
        would erase the argument policy and widen authority.  Schema v1
        therefore admits only a selector that the shared command-grant
        compiler can preserve exactly.
        """

        from skills.command_grants import command_grant_from_selector

        if (
            not _NATIVE_SELECTOR_RE.fullmatch(selector)
            or command_grant_from_selector(selector) is None
        ):
            self.error(
                "knowledge_gate_narrowed_selector_unsupported",
                "A parenthesized knowledge-gate selector must be one safe, "
                "exact Bash/Shell command grant such as Bash(python:*); "
                "other argument policies are unsupported in schema v1.",
                field=field,
                selector=selector,
            )
            return ""
        return selector

    def _canonical_native_selector(self, value: str, *, field: str) -> str:
        selector = value.strip()
        if selector.casefold().startswith("skill:"):
            return self._canonical_selector(selector, field=field)
        if "(" in selector:
            return self._canonical_narrowed_selector(
                selector,
                field=field,
            )
        if (
            not selector
            or len(selector) > MAX_GATE_SELECTOR_CHARS
            or not _NATIVE_SELECTOR_RE.fullmatch(selector)
        ):
            self.error(
                "knowledge_gate_native_selector_invalid",
                "A knowledge-gate native selector must use one bounded exact selector spelling.",
                field=field,
                selector=selector,
            )
            return ""
        return selector

    def _selector_from_descriptor(
        self,
        descriptor: dict[Any, Any],
        *,
        field: str,
    ) -> str:
        unknown = sorted(
            str(key)
            for key in descriptor
            if str(key) not in _SELECTOR_DESCRIPTOR_KEYS
        )
        if unknown:
            self.error(
                "knowledge_gate_selector_descriptor_key_unknown",
                "A knowledge-gate selector descriptor contains unknown execution keys.",
                field=field,
                unknown_keys=unknown,
            )
            return ""
        if "optional" in descriptor or "required" in descriptor:
            self.error(
                "knowledge_gate_selector_requirement_modifier_unsupported",
                "Knowledge-gate selector descriptors cannot use optional/required "
                "modifiers in schema v1; conditionality must be expressed by "
                "an explicit branch and AND-of-OR tool groups.",
                field=field,
            )
            return ""

        generic_coordinates: list[str] = []
        for key in ("tool", "selector", "tool_selector"):
            if descriptor.get(key) not in (None, ""):
                generic_coordinates.append(str(descriptor.get(key)).strip())

        skill_coordinates: list[str] = []
        if descriptor.get("skill") not in (None, ""):
            skill = str(descriptor.get("skill")).strip()
            skill_coordinates.append(
                skill if skill.casefold().startswith("skill:") else f"skill:{skill}"
            )

        local_coordinates: list[str] = []
        for key in ("path", "file", "resource"):
            if descriptor.get(key) not in (None, ""):
                local_coordinates.append(str(descriptor.get(key)).strip())

        source = str(descriptor.get("source") or "").strip()
        if source.casefold().startswith("skill:"):
            skill_coordinates.append(source)
        elif source.casefold() in _LOCAL_SOURCE_NAMES:
            pass
        elif not (
            generic_coordinates or skill_coordinates or local_coordinates
        ):
            # ``source`` is commonly provenance metadata when a descriptor
            # also names a tool.  With no stronger coordinate, prefer the
            # explicit name and otherwise treat source as the native selector.
            fallback = descriptor.get("name")
            generic_coordinates.append(
                str(fallback if fallback not in (None, "") else source).strip()
            )

        generic_coordinates = list(dict.fromkeys(generic_coordinates))
        skill_coordinates = list(dict.fromkeys(skill_coordinates))
        local_coordinates = list(dict.fromkeys(local_coordinates))
        coordinate_count = (
            len(generic_coordinates)
            + len(skill_coordinates)
            + len(local_coordinates)
        )
        if coordinate_count != 1:
            self.error(
                "knowledge_gate_selector_descriptor_ambiguous",
                "A knowledge-gate selector descriptor must resolve to exactly "
                "one symbolic coordinate.",
                field=field,
                coordinate_count=coordinate_count,
            )
            return ""
        if local_coordinates:
            return self._record_local_selector(
                local_coordinates[0],
                field=field,
            )
        if skill_coordinates:
            return self._canonical_selector(
                skill_coordinates[0],
                field=field,
            )
        return self._canonical_native_selector(
            generic_coordinates[0],
            field=field,
        )

    def _selector(self, value: Any, *, field: str) -> str:
        if isinstance(value, dict):
            return self._selector_from_descriptor(value, field=field)
        return self._canonical_selector(value, field=field)

    def _selector_list(self, value: Any, *, field: str) -> list[str]:
        if isinstance(value, (str, dict)):
            raw = [value]
        elif isinstance(value, (list, tuple, set)):
            raw = list(value)
            if isinstance(value, set):
                raw.sort(key=str)
        else:
            self.error(
                "knowledge_gate_selector_list_invalid",
                "A knowledge-gate selector group must contain a scalar or sequence.",
                field=field,
                value_type=type(value).__name__,
            )
            return []
        if not raw:
            self.error(
                "knowledge_gate_selector_group_empty",
                "A declared one_of selector group cannot be empty.",
                field=field,
            )
            return []
        if len(raw) > MAX_GATE_SELECTORS_PER_GROUP:
            self.error(
                "knowledge_gate_selector_group_limit_exceeded",
                "A knowledge-gate one_of group contains too many selectors.",
                field=field,
                limit=MAX_GATE_SELECTORS_PER_GROUP,
                actual=len(raw),
            )
            raw = raw[:MAX_GATE_SELECTORS_PER_GROUP]
        remaining = max(0, MAX_GATE_TOTAL_SELECTORS - self.total_selectors)
        if len(raw) > remaining:
            if not self.selector_limit_failed:
                self.selector_limit_failed = True
                self.error(
                    "knowledge_gate_total_selector_limit_exceeded",
                    "The worker knowledge gate contains too many symbolic selectors.",
                    field=field,
                    limit=MAX_GATE_TOTAL_SELECTORS,
                    actual=self.total_selectors + len(raw),
                )
            raw = raw[:remaining]
        selectors: list[str] = []
        for index, item in enumerate(raw):
            if isinstance(item, dict) and _TOOL_EXPRESSION_KEYS.intersection(
                str(key) for key in item
            ):
                self.error(
                    "knowledge_gate_nested_expression_invalid",
                    "Nested tool expressions must appear under all_of/tool_groups, "
                    "not inside one any_of group.",
                    field=f"{field}[{index}]",
                )
                continue
            selector = self._selector(item, field=f"{field}[{index}]")
            if selector:
                selectors.append(selector)
        selectors = list(dict.fromkeys(selectors))
        self.total_selectors += len(selectors)
        return selectors

    def _group(self, selectors: list[str]) -> dict[str, Any] | None:
        if not selectors:
            return None
        return {"mode": "one_of", "selectors": selectors}

    def _groups_from_tools(
        self,
        value: Any,
        *,
        field: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Lower tools grammar and report whether it used legacy ambiguity."""

        if value in (None, "", [], (), set()):
            return [], False
        if value == {}:
            self.error(
                "knowledge_gate_tools_invalid",
                "An empty tools mapping is not a selector or any_of/all_of expression.",
                field=field,
            )
            return [], False
        if isinstance(value, dict) and _TOOL_EXPRESSION_KEYS.intersection(
            str(key) for key in value
        ):
            keys = {str(key) for key in value}
            unknown = sorted(keys - _TOOL_EXPRESSION_KEYS)
            if unknown or len(keys.intersection(_TOOL_EXPRESSION_KEYS)) != 1:
                self.error(
                    "knowledge_gate_tool_expression_key_invalid",
                    "A tools expression must contain exactly one of any_of or "
                    "all_of and no other keys.",
                    field=field,
                    unknown_keys=unknown,
                )
                return [], False
            if "any_of" in value:
                group = self._group(
                    self._selector_list(value.get("any_of"), field=f"{field}.any_of")
                )
                return ([group] if group else []), False
            groups = self._groups_from_all_of(
                value.get("all_of"),
                field=f"{field}.all_of",
            )
            return groups, False
        if isinstance(value, dict):
            # A single structured selector remains a legacy one-of group.
            group = self._group(self._selector_list(value, field=field))
            return ([group] if group else []), True
        if isinstance(value, (str, list, tuple, set)):
            group = self._group(self._selector_list(value, field=field))
            return ([group] if group else []), True
        self.error(
            "knowledge_gate_tools_invalid",
            "Knowledge-gate tools must be a selector, sequence, or any_of/all_of mapping.",
            field=field,
            value_type=type(value).__name__,
        )
        return [], False

    def _groups_from_all_of(
        self,
        value: Any,
        *,
        field: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, (list, tuple, set)):
            self.error(
                "knowledge_gate_all_of_invalid",
                "A knowledge-gate all_of expression must be a sequence.",
                field=field,
            )
            return []
        entries = list(value)
        if isinstance(value, set):
            entries.sort(key=str)
        if len(entries) > MAX_GATE_GROUPS_PER_BRANCH:
            self.error(
                "knowledge_gate_group_limit_exceeded",
                "A knowledge-gate all_of expression declares too many selector groups.",
                field=field,
                limit=MAX_GATE_GROUPS_PER_BRANCH,
                actual=len(entries),
            )
            entries = entries[:MAX_GATE_GROUPS_PER_BRANCH]
        groups: list[dict[str, Any]] = []
        for index, item in enumerate(entries):
            item_field = f"{field}[{index}]"
            if isinstance(item, dict) and "any_of" in item:
                unknown = sorted(str(key) for key in item if str(key) != "any_of")
                if unknown:
                    self.error(
                        "knowledge_gate_tool_expression_key_invalid",
                        "An all_of member's any_of expression contains unknown keys.",
                        field=item_field,
                        unknown_keys=unknown,
                    )
                    continue
                group = self._group(
                    self._selector_list(
                        item.get("any_of"),
                        field=f"{item_field}.any_of",
                    )
                )
                if group:
                    groups.append(group)
                continue
            if isinstance(item, dict) and "all_of" in item:
                unknown = sorted(str(key) for key in item if str(key) != "all_of")
                if unknown:
                    self.error(
                        "knowledge_gate_tool_expression_key_invalid",
                        "A nested all_of expression contains unknown keys.",
                        field=item_field,
                        unknown_keys=unknown,
                    )
                    continue
                groups.extend(
                    self._groups_from_all_of(
                        item.get("all_of"),
                        field=f"{item_field}.all_of",
                    )
                )
                continue
            if isinstance(item, (list, tuple, set)):
                self.error(
                    "knowledge_gate_all_of_member_ambiguous",
                    "A bare sequence inside all_of is ambiguous; wrap alternatives in any_of.",
                    field=item_field,
                )
                continue
            group = self._group(self._selector_list(item, field=item_field))
            if group:
                groups.append(group)
        return groups

    def _groups_from_explicit_tool_groups(
        self,
        value: Any,
        *,
        field: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, (list, tuple, set)):
            self.error(
                "knowledge_gate_tool_groups_invalid",
                "Explicit tool_groups must be a sequence of one_of groups.",
                field=field,
            )
            return []
        entries = list(value)
        if isinstance(value, set):
            entries.sort(key=str)
        if len(entries) > MAX_GATE_GROUPS_PER_BRANCH:
            self.error(
                "knowledge_gate_group_limit_exceeded",
                "A knowledge-gate branch declares too many selector groups.",
                field=field,
                limit=MAX_GATE_GROUPS_PER_BRANCH,
                actual=len(entries),
            )
            entries = entries[:MAX_GATE_GROUPS_PER_BRANCH]
        groups: list[dict[str, Any]] = []
        for index, item in enumerate(entries):
            item_field = f"{field}[{index}]"
            if isinstance(item, dict):
                keys = {str(key) for key in item}
                unknown = sorted(keys - _TOOL_GROUP_KEYS)
                if unknown:
                    self.error(
                        "knowledge_gate_tool_group_key_unknown",
                        "A knowledge-gate tool group contains unknown execution keys.",
                        field=item_field,
                        unknown_keys=unknown,
                    )
                    continue
                mode = str(item.get("mode") or "one_of").strip().casefold()
                if mode not in {"one_of", "any", "any_of"}:
                    self.error(
                        "knowledge_gate_tool_group_mode_invalid",
                        "Every explicit tool_group must use one_of semantics.",
                        field=f"{item_field}.mode",
                        mode=mode,
                    )
                    continue
                expression_keys = [
                    key
                    for key in ("any_of", "all_of", "selectors", "tools")
                    if key in item
                ]
                if len(expression_keys) != 1:
                    self.error(
                        "knowledge_gate_tool_group_expression_invalid",
                        "A tool_group must contain exactly one "
                        "selectors/tools/any_of/all_of expression.",
                        field=item_field,
                    )
                    continue
                expression_key = expression_keys[0]
                if expression_key == "all_of":
                    groups.extend(
                        self._groups_from_all_of(
                            item.get("all_of"),
                            field=f"{item_field}.all_of",
                        )
                    )
                    continue
                selectors = self._selector_list(
                    item.get(expression_key),
                    field=f"{item_field}.{expression_key}",
                )
            else:
                selectors = self._selector_list(item, field=item_field)
            group = self._group(selectors)
            if group:
                if isinstance(item, dict):
                    group_id = str(item.get("id") or "").strip()
                    if group_id:
                        if not is_canonical_knowledge_gate_identifier(group_id):
                            self.error(
                                "knowledge_gate_tool_group_id_invalid",
                                "A knowledge-gate tool-group id must be one "
                                "canonical bounded Unicode identifier.",
                                field=f"{item_field}.id",
                            )
                        else:
                            group["id"] = group_id
                groups.append(group)
        return groups

    def _declared_groups(
        self,
        mapping: dict[Any, Any],
        *,
        field: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        has_tools = "tools" in mapping
        has_groups = "tool_groups" in mapping
        if has_tools and has_groups:
            self.error(
                "knowledge_gate_tool_declaration_conflict",
                "A knowledge-gate scope cannot declare both tools and tool_groups.",
                field=field,
            )
            return [], False
        if has_groups:
            return (
                self._groups_from_explicit_tool_groups(
                    mapping.get("tool_groups"),
                    field=f"{field}.tool_groups",
                ),
                False,
            )
        if has_tools:
            return self._groups_from_tools(
                mapping.get("tools"),
                field=f"{field}.tools",
            )
        return [], False

    def _branch(
        self,
        *,
        check_id: str,
        outcome: str,
        raw_branch: Any,
        inherited_groups: list[dict[str, Any]],
        inherited_legacy: bool,
        field: str,
    ) -> tuple[dict[str, Any], bool]:
        local_declaration = False
        if isinstance(raw_branch, dict):
            unknown = sorted(
                str(key)
                for key in raw_branch
                if str(key) not in _BRANCH_MAPPING_KEYS
            )
            if unknown:
                self.error(
                    "knowledge_gate_branch_key_unknown",
                    "A knowledge-gate branch contains unknown execution keys.",
                    field=field,
                    unknown_keys=unknown,
                )
            action_keys = [
                key
                for key in ("action", "instruction", "description")
                if raw_branch.get(key) not in (None, "")
            ]
            if len(action_keys) > 1:
                self.error(
                    "knowledge_gate_branch_action_conflict",
                    "A knowledge-gate branch must use only one action text field.",
                    field=field,
                    action_keys=action_keys,
                )
            action = self._bounded_text(
                raw_branch.get(action_keys[0]) if action_keys else "",
                field=f"{field}.{action_keys[0] if action_keys else 'action'}",
            )
            local_declaration = (
                "tools" in raw_branch or "tool_groups" in raw_branch
            )
            if local_declaration:
                groups, legacy = self._declared_groups(
                    raw_branch,
                    field=field,
                )
            elif not action:
                groups = []
                legacy = False
            else:
                groups = json.loads(json.dumps(inherited_groups))
                legacy = inherited_legacy
        else:
            action = self._bounded_text(raw_branch, field=field)
            if raw_branch in (None, ""):
                groups = []
                legacy = False
            else:
                groups = json.loads(json.dumps(inherited_groups))
                legacy = inherited_legacy

        if len(groups) > MAX_GATE_GROUPS_PER_BRANCH:
            self.error(
                "knowledge_gate_group_limit_exceeded",
                "A knowledge-gate branch contains too many selector groups.",
                field=field,
                limit=MAX_GATE_GROUPS_PER_BRANCH,
                actual=len(groups),
            )
            groups = groups[:MAX_GATE_GROUPS_PER_BRANCH]
        remaining_groups = max(
            0,
            MAX_GATE_TOTAL_GROUPS - self.total_groups,
        )
        if len(groups) > remaining_groups:
            if not self.group_limit_failed:
                self.group_limit_failed = True
                self.error(
                    "knowledge_gate_total_group_limit_exceeded",
                    "The worker knowledge gate contains too many conditional selector groups.",
                    field=field,
                    limit=MAX_GATE_TOTAL_GROUPS,
                    actual=self.total_groups + len(groups),
                )
            groups = groups[:remaining_groups]
        self.total_groups += len(groups)
        normalized_groups: list[dict[str, Any]] = []
        for index, group in enumerate(groups):
            normalized = dict(group)
            normalized.setdefault(
                "id",
                f"{check_id}:{outcome}:group-{index + 1}",
            )
            normalized_groups.append(normalized)
        branch = {
            "outcome": outcome,
            "action": action,
            "selector_groups": normalized_groups,
        }
        if local_declaration:
            branch["branch_local_tools"] = True
        return branch, legacy

    def _checks(self, raw_checks: Any) -> list[dict[str, Any]]:
        entries: list[tuple[Any, Any, str]] = []
        if isinstance(raw_checks, dict):
            for index, (key, value) in enumerate(raw_checks.items()):
                entries.append((key, value, f"knowledge_gate.checks.{key}"))
        elif isinstance(raw_checks, (list, tuple)):
            for index, value in enumerate(raw_checks):
                entries.append((None, value, f"knowledge_gate.checks[{index}]"))
        else:
            self.error(
                "knowledge_gate_checks_invalid",
                "knowledge_gate.checks must be a list or id-keyed mapping.",
                field="knowledge_gate.checks",
                value_type=type(raw_checks).__name__,
            )
            return []
        if len(entries) > MAX_GATE_CHECKS:
            self.error(
                "knowledge_gate_check_limit_exceeded",
                "A worker declares too many knowledge-gate checks.",
                field="knowledge_gate.checks",
                limit=MAX_GATE_CHECKS,
                actual=len(entries),
            )
            entries = entries[:MAX_GATE_CHECKS]

        checks: list[dict[str, Any]] = []
        seen_ids: dict[str, str] = {}
        for index, (mapping_id, raw_check, field) in enumerate(entries):
            if isinstance(raw_check, str):
                check_mapping: dict[str, Any] = {"question": raw_check}
            elif isinstance(raw_check, dict):
                check_mapping = raw_check
            else:
                self.error(
                    "knowledge_gate_check_invalid",
                    "Every knowledge-gate check must be a string or mapping.",
                    field=field,
                    value_type=type(raw_check).__name__,
                )
                continue
            unknown = sorted(
                str(key)
                for key in check_mapping
                if str(key) not in _CHECK_KEYS
            )
            if unknown:
                self.error(
                    "knowledge_gate_check_key_unknown",
                    "A knowledge-gate check contains unknown execution keys.",
                    field=field,
                    unknown_keys=unknown,
                )
            explicit_id = check_mapping.get("id") or check_mapping.get("name")
            if mapping_id is not None and explicit_id not in (None, ""):
                if str(mapping_id).strip() != str(explicit_id).strip():
                    self.error(
                        "knowledge_gate_check_id_conflict",
                        "An id-keyed knowledge-gate check conflicts with its embedded id.",
                        field=field,
                        mapping_id=str(mapping_id),
                        embedded_id=str(explicit_id),
                    )
            raw_id = (
                mapping_id
                if mapping_id not in (None, "")
                else explicit_id
                if explicit_id not in (None, "")
                else f"check-{index + 1}"
            )
            check_id = self._canonical_check_id(raw_id, field=f"{field}.id")
            if not check_id:
                continue
            folded_id = check_id.casefold()
            if folded_id in seen_ids:
                self.error(
                    "duplicate_knowledge_gate_check_id",
                    "A worker knowledge gate repeats a check id.",
                    field=f"{field}.id",
                    check_id=check_id,
                    first_field=seen_ids[folded_id],
                )
                continue
            seen_ids[folded_id] = f"{field}.id"

            question_value = (
                check_mapping.get("question")
                if check_mapping.get("question") is not None
                else check_mapping.get("description")
            )
            question = self._bounded_text(
                question_value,
                field=f"{field}.question",
                required=True,
            )
            inherited_groups, inherited_legacy = self._declared_groups(
                check_mapping,
                field=field,
            )
            branch_keys = [
                key for key in _BRANCH_KEYS if key in check_mapping
            ]
            if inherited_groups and not branch_keys:
                self.error(
                    "knowledge_gate_tools_without_branch",
                    "Knowledge-gate tools require an explicit "
                    "if_yes/if_no/if_unknown activation branch.",
                    field=field,
                )
            branches: list[dict[str, Any]] = []
            check_legacy = False
            for branch_key, outcome in _BRANCH_KEYS.items():
                if branch_key not in check_mapping:
                    continue
                branch, branch_legacy = self._branch(
                    check_id=check_id,
                    outcome=outcome,
                    raw_branch=check_mapping.get(branch_key),
                    inherited_groups=inherited_groups,
                    inherited_legacy=inherited_legacy,
                    field=f"{field}.{branch_key}",
                )
                branches.append(branch)
                check_legacy = check_legacy or branch_legacy
            checks.append({
                "id": check_id,
                "question": question,
                "branches": branches,
                "legacy_ambiguous": check_legacy,
            })
        return checks

    def compile(self, knowledge_gate: Any) -> SymbolicKnowledgeGateCompilation:
        if knowledge_gate in (None, {}):
            return SymbolicKnowledgeGateCompilation({}, (), (), ())
        if not isinstance(knowledge_gate, dict):
            self.error(
                "knowledge_gate_invalid",
                "A worker knowledge_gate declaration must be a mapping.",
                field="knowledge_gate",
                value_type=type(knowledge_gate).__name__,
            )
            return self.result([])
        if not self._bounded_graph(knowledge_gate, field="knowledge_gate"):
            return self.result([])
        unknown = sorted(
            str(key) for key in knowledge_gate if str(key) not in _GATE_KEYS
        )
        if unknown:
            self.error(
                "knowledge_gate_key_unknown",
                "A worker knowledge_gate contains unknown execution keys.",
                field="knowledge_gate",
                unknown_keys=unknown,
            )
        if "checks" not in knowledge_gate:
            self.error(
                "knowledge_gate_checks_missing",
                "A worker knowledge_gate requires a checks declaration.",
                field="knowledge_gate.checks",
            )
            return self.result([])
        checks = self._checks(knowledge_gate.get("checks"))
        return self.result(checks, knowledge_gate=knowledge_gate)

    def result(
        self,
        checks: list[dict[str, Any]],
        *,
        knowledge_gate: dict[str, Any] | None = None,
    ) -> SymbolicKnowledgeGateCompilation:
        skill_refs = list(dict.fromkeys(self.skill_refs))
        local_resources = list(dict.fromkeys(self.local_resources))
        if len(local_resources) > MAX_GATE_LOCAL_RESOURCES:
            self.error(
                "knowledge_gate_local_resource_limit_exceeded",
                "A worker knowledge gate resolves to too many local package resources.",
                field="knowledge_gate.local_resources",
                limit=MAX_GATE_LOCAL_RESOURCES,
                actual=len(local_resources),
            )
            local_resources = local_resources[:MAX_GATE_LOCAL_RESOURCES]
        expansions: list[dict[str, Any]] = []
        seen_expansions: set[str] = set()
        for row in self.resource_expansions:
            selector = str(row.get("selector") or "")
            if not selector or selector in seen_expansions:
                continue
            seen_expansions.add(selector)
            expansions.append({
                "selector": selector,
                "resources": [
                    path
                    for path in row.get("resources") or []
                    if path in set(local_resources)
                ],
            })
        description = ""
        policy = ""
        if knowledge_gate is not None:
            description = self._bounded_text(
                knowledge_gate.get("description"),
                field="knowledge_gate.description",
            )
            policy = self._bounded_text(
                knowledge_gate.get("mid_execution_search_policy"),
                field="knowledge_gate.mid_execution_search_policy",
            )
        errors = sum(
            diagnostic.level == "errors" for diagnostic in self.diagnostics
        )
        warnings = sum(
            diagnostic.level == "warnings" for diagnostic in self.diagnostics
        )
        ir: dict[str, Any] = {
            "schema_version": KNOWLEDGE_GATE_SCHEMA_VERSION,
            "source_file": self.source_file,
            "checks": checks,
            "skill_refs": skill_refs,
            "local_resources": local_resources,
            "resource_expansions": expansions,
            "valid": errors == 0,
            "diagnostic_summary": {
                "error_count": errors,
                "warning_count": warnings,
            },
        }
        if description:
            ir["description"] = description
        if policy:
            ir["mid_execution_search_policy"] = policy
        digest_projection = {
            key: value
            for key, value in ir.items()
            if key not in {"diagnostic_summary", "valid"}
        }
        ir["ir_sha256"] = hashlib.sha256(
            json.dumps(
                digest_projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return SymbolicKnowledgeGateCompilation(
            ir=ir,
            skill_refs=tuple(skill_refs),
            local_resources=tuple(local_resources),
            diagnostics=tuple(self.diagnostics),
        )


def compile_symbolic_knowledge_gate(
    knowledge_gate: Any,
    *,
    skill_dir: Path,
    source_file: str | None,
    worker_id: str,
) -> SymbolicKnowledgeGateCompilation:
    """Compile one worker gate without resolving runtime capability candidates."""

    return _Compiler(
        skill_dir=skill_dir,
        source_file=source_file,
        worker_id=worker_id,
    ).compile(knowledge_gate)


__all__ = [
    "KNOWLEDGE_GATE_SCHEMA_VERSION",
    "MAX_GATE_IDENTIFIER_CHARS",
    "KnowledgeGateDiagnostic",
    "SymbolicKnowledgeGateCompilation",
    "compile_symbolic_knowledge_gate",
    "is_canonical_knowledge_gate_identifier",
]
