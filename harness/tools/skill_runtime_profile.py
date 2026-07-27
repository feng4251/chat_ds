"""Exact, snapshot-bound runtime selection for authorized Skill entrypoints.

Runtime selection is a security decision because the browser profile receives
access to the policy egress proxy.  This module therefore consumes only one
immutable :class:`SkillPackageSnapshot`: the same bytes are used for package
authority, reachable-source inspection, executor selection and process-open
serialization.  Natural-language Skill prose and model arguments are never
runtime authority.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import shlex
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from skills.loader import parse_frontmatter
from tools.isolated_skill_executor import (
    EXECUTOR_SOCKET,
    IsolatedSkillExecutorError,
    SkillPackageSnapshot,
)


BASE_RUNTIME_PROFILE = "base-v1"
BROWSER_RUNTIME_PROFILE = "browser-automation-v1"
SUPPORTED_RUNTIME_PROFILES = frozenset({
    BASE_RUNTIME_PROFILE,
    BROWSER_RUNTIME_PROFILE,
})

MAX_PROFILE_SOURCE_FILES = 1_024
MAX_PROFILE_SOURCE_BYTES = 24 * 1024 * 1024
MAX_PROFILE_SOURCE_FILE_BYTES = 1_000_000
MAX_PACKAGE_JSON_BYTES = 256_000
SKILL_RUNTIME_MANIFEST_NAME = "chatds-runtime.json"
MAX_SKILL_RUNTIME_MANIFEST_BYTES = 256_000
MAX_SKILL_RUNTIME_MANIFEST_ENTRYPOINTS = 40
MAX_SHELL_HEREDOCS = 128
MAX_SHELL_HEREDOC_BODY_BYTES = MAX_PROFILE_SOURCE_FILE_BYTES

_SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".sh", ".bash",
})
_BROWSER_PYTHON_IMPORTS = frozenset({"playwright", "selenium"})
_UNSUPPORTED_BROWSER_PYTHON_IMPORTS = frozenset({"pyppeteer"})
_BROWSER_NODE_PACKAGES = frozenset({"playwright", "playwright-core"})
_UNSUPPORTED_BROWSER_NODE_PACKAGES = frozenset({
    "@playwright/test",
    "puppeteer",
    "puppeteer-core",
    "selenium-webdriver",
})
_BROWSER_REQUIREMENT_NAMES = frozenset({"playwright", "selenium"})
_UNSUPPORTED_BROWSER_REQUIREMENT_NAMES = frozenset({"pyppeteer"})
# Import names and distribution names are not universally identical.  Keep a
# deliberately small, runtime-owned alias table for common unambiguous cases;
# identity matches remain the general path and ambiguous namespace packages
# require an explicit dynamic-entrypoint declaration.
_PYTHON_IMPORT_REQUIREMENT_ALIASES = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "pil": "pillow",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
}
_NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster",
    "console", "constants", "crypto", "dgram", "diagnostics_channel",
    "dns", "domain", "events", "fs", "http", "http2", "https",
    "module", "net", "os", "path", "perf_hooks", "process",
    "punycode", "querystring", "readline", "repl", "stream",
    "string_decoder", "sys", "timers", "tls", "trace_events", "tty",
    "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
})
_FIXED_NODE_PACKAGES = {
    BASE_RUNTIME_PROFILE: frozenset(),
    BROWSER_RUNTIME_PROFILE: frozenset({
        "playwright",
        "playwright-core",
    }),
}
_FIXED_NODE_PACKAGE_VERSIONS = {
    BROWSER_RUNTIME_PROFILE: {
        "playwright": "1.61.0",
        "playwright-core": "1.61.0",
    },
}
_FIXED_PYTHON_PACKAGE_VERSIONS = {
    BROWSER_RUNTIME_PROFILE: {
        "playwright": "1.61.0",
        "selenium": "4.46.0",
    },
}
_SHELL_SKILL_PREFIXES = (
    "${CHATDS_SKILL_DIR}/",
    "$CHATDS_SKILL_DIR/",
    "${CHATDS_SKILL_ROOT}/",
    "$CHATDS_SKILL_ROOT/",
    "${SKILL_DIR}/",
    "$SKILL_DIR/",
)
_SHELL_COMMAND_SEPARATORS = frozenset({
    ";", ";;", ";&", ";;&", "&", "&&", "|", "||",
})
_SHELL_COMMENT_BOUNDARY_CHARS = frozenset(";&|(){}")
_SHELL_BUILTINS = frozenset({
    ".", ":", "[", "alias", "bg", "break", "builtin", "cd", "command",
    "continue", "declare", "dirs", "disown", "echo", "enable", "eval",
    "exec", "exit", "export", "false", "fc", "fg", "getopts", "hash",
    "help", "history", "jobs", "kill", "let", "local", "logout", "mapfile",
    "popd", "printf", "pushd", "pwd", "read", "readarray", "readonly",
    "return", "set", "shift", "shopt", "source", "suspend", "test", "times",
    "trap", "true", "type", "typeset", "ulimit", "umask", "unalias",
    "unset", "wait",
})
_SHELL_CONTROL_PREFIXES = frozenset({
    "!", "{", "do", "elif", "else", "if", "then", "time", "until",
    "while",
})
_SHELL_CONTROL_ONLY = frozenset({
    "}", "case", "coproc", "done", "esac", "fi", "for", "function", "in",
    "select",
})
# Pipeline-fed input is allowed without a profile marker only for literal,
# non-dispatching stream consumers (or an exact local script analyzed in the
# source closure). Unknown stages, shell functions, and dispatch utilities
# remain fail-closed because a bounded tokenizer cannot prove their stdin is
# data rather than synthesized code.
_SHELL_BOUNDED_PIPE_STDIN_CONSUMERS = frozenset({
    "base64", "cat", "cut", "egrep", "fgrep", "fold", "grep", "head",
    "jq", "nl", "od", "paste", "sed", "sort", "tail", "tee", "tr",
    "uniq", "wc",
})
_SHELL_STDIN_DISPATCH_COMMANDS = frozenset({"parallel", "xargs"})
_PYTHON_COMMAND_NAMES = frozenset({
    "python", "python3", "python3.10", "python3.11", "python3.12",
    "python3.13",
})
_NODE_COMMAND_NAMES = frozenset({"node", "nodejs"})
_SHELL_COMMAND_NAMES = frozenset({"bash", "dash", "sh", "zsh"})
_LITERAL_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,80}$")
# Both fixed executor profiles attest these shell paths and ``/usr/bin/env``.
# Python/Node live under different absolute prefixes across profiles, so their
# direct scripts must use env/PATH and the ordinary command preflight.
_FIXED_SHEBANG_LAUNCHERS = frozenset({
    "/bin/bash",
    "/bin/dash",
    "/bin/sh",
    "/usr/bin/bash",
    "/usr/bin/dash",
    "/usr/bin/env",
    "/usr/bin/sh",
})
_JS_MODULE_RE = re.compile(
    r"""(?x)
    (?:
        \bfrom\s*
        |\bimport\s*\(\s*
        |\brequire\s*\(\s*
        |\bimport\s+
    )
    ["']([^"']{1,200})["']
    """
)
_JS_DYNAMIC_MODULE_CALL_RE = re.compile(
    r"""(?x)
    \b(?:import|require)\s*\(\s*(?!["'])
    """
)
_JS_DYNAMIC_EXECUTION_RE = re.compile(
    r"""(?x)
    \b(?:
        eval
        |process\s*\.\s*chdir
        |(?:child_process\s*\.\s*)?
          (?:spawn|spawnSync|exec|execSync|execFile|execFileSync|fork)
    )\s*\(
    """
)
_JS_CWD_MUTATION_RE = re.compile(
    r"\bprocess\s*\.\s*chdir\s*\("
)
_NUMERIC_SEMVER_RE = re.compile(
    r"^(?:v)?([0-9]+)\.([0-9]+)\.([0-9]+)$"
)
_RUNTIME_PROFILE_MARKER_RE = re.compile(
    r"(?m)^\s*(?://|#|/\*)?\s*"
    r"(?:CHATDS_RUNTIME_PROFILE|chatds-runtime-profile)\s*[:=]\s*"
    r"([A-Za-z0-9_.-]{1,80})\s*(?:\*/)?\s*$",
)


@dataclass(frozen=True, slots=True)
class SkillRuntimeSelection:
    """One exact entrypoint's immutable routing/capability identity."""

    runtime_profile: str
    package_sha256: str
    entrypoint: str
    script_sha256: str
    reachable_sources: tuple[str, ...]
    runtime_requirements: tuple[str, ...]
    runtime_commands: tuple[str, ...]
    runtime_node_packages: tuple[str, ...]
    required_cwd: str | None
    runtime_manifest_path: str | None
    runtime_manifest_sha256: str | None


@dataclass(frozen=True, slots=True)
class RuntimeProfileSocketBinding:
    """Runtime-owned UDS selection; the model never supplies these fields."""

    runtime_profile: str
    socket_path: str
    socket_identity_sha256: str


@dataclass(frozen=True, slots=True)
class _DynamicSourceAnalysis:
    """Bounded evidence extracted from code-loading/dispatch constructs."""

    local_dependencies: frozenset[str] = frozenset()
    python_import_roots: frozenset[str] = frozenset()
    node_packages: frozenset[str] = frozenset()
    dynamic_dependency: bool = False
    runtime_commands: frozenset[str] = frozenset()
    required_cwds: frozenset[str] = frozenset()
    cwd_mutated: bool = False


@dataclass(frozen=True, slots=True)
class _EntrypointRuntimeDeclaration:
    """One strict package-root manifest declaration for an exact script."""

    runtime_profile: str
    python_requirements: tuple[str, ...]
    node_packages: tuple[tuple[str, str], ...]
    runtime_commands: tuple[str, ...]
    manifest_path: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _ShellHeredocDescriptor:
    delimiter: str
    strip_tabs: bool
    quoted: bool
    interpreter_input: bool


@dataclass(frozen=True, slots=True)
class _ShellHeredocPreprocess:
    source: str
    dynamic_dependency: bool


def _unsupported_browser_dependency(name: str) -> None:
    raise IsolatedSkillExecutorError(
        "browser_runtime_dependency_unsupported",
        f"The exact Skill requires browser dependency {name!r}, but the "
        "fixed browser runtime does not attest that package.",
    )


def _unsupported_runtime_profile(value: Any) -> None:
    raise IsolatedSkillExecutorError(
        "skill_runtime_profile_unsupported",
        f"The exact Skill declares unsupported runtime profile {value!r}.",
    )


def _unsupported_dependency_declaration(
    name: str,
    detail: str,
) -> None:
    raise IsolatedSkillExecutorError(
        "skill_runtime_dependency_declaration_unsupported",
        f"Dependency {name!r} cannot be proven against the fixed isolated "
        f"runtime: {detail}",
    )


def _invalid_runtime_manifest(detail: str) -> None:
    raise IsolatedSkillExecutorError(
        "skill_runtime_manifest_invalid",
        "The exact Skill runtime manifest is invalid: " + detail,
    )


def _runtime_profile_conflict(detail: str) -> None:
    raise IsolatedSkillExecutorError(
        "skill_runtime_profile_conflict",
        "The exact Skill has conflicting runtime profile authority: "
        + detail,
    )


def runtime_profile_socket_binding(
    runtime_profile: str,
) -> RuntimeProfileSocketBinding:
    """Resolve one supported profile to a runtime-owned Unix socket."""

    if runtime_profile not in SUPPORTED_RUNTIME_PROFILES:
        _unsupported_runtime_profile(runtime_profile)
    if runtime_profile == BROWSER_RUNTIME_PROFILE:
        socket_path = os.environ.get(
            "SKILL_BROWSER_EXECUTOR_SOCKET", ""
        ).strip()
        if not socket_path:
            raise IsolatedSkillExecutorError(
                "browser_runtime_unavailable",
                "The isolated browser runtime socket is not configured.",
            )
    else:
        socket_path = os.environ.get(
            "EXECUTOR_SOCKET", EXECUTOR_SOCKET
        ).strip()
        if not socket_path:
            raise IsolatedSkillExecutorError(
                "executor_unavailable",
                "The isolated Skill executor socket is not configured.",
            )
    identity = hashlib.sha256(
        (
            "chatds-runtime-socket-v1\0"
            + runtime_profile
            + "\0"
            + socket_path
        ).encode("utf-8")
    ).hexdigest()
    return RuntimeProfileSocketBinding(
        runtime_profile=runtime_profile,
        socket_path=socket_path,
        socket_identity_sha256=identity,
    )


def _snapshot_sources(
    snapshot: SkillPackageSnapshot,
) -> dict[str, bytes]:
    if not isinstance(snapshot, SkillPackageSnapshot):
        raise IsolatedSkillExecutorError(
            "invalid_skill_snapshot",
            "Runtime selection requires an immutable Skill package snapshot.",
        )
    sources: dict[str, bytes] = {}
    total = 0
    for path in snapshot.paths:
        suffix = PurePosixPath(path).suffix.casefold()
        if suffix not in _SOURCE_SUFFIXES:
            continue
        content = snapshot.read_bytes(path)
        if len(content) > MAX_PROFILE_SOURCE_FILE_BYTES:
            raise IsolatedSkillExecutorError(
                "skill_script_inspection_limit",
                "A reachable Skill source exceeds the profile-inspection limit.",
            )
        total += len(content)
        if (
            len(sources) >= MAX_PROFILE_SOURCE_FILES
            or total > MAX_PROFILE_SOURCE_BYTES
        ):
            raise IsolatedSkillExecutorError(
                "skill_runtime_profile_limit",
                "The Skill source inventory exceeds its bounded profile limit.",
            )
        sources[path] = content
    return sources


def _decode_source(path: str, sources: dict[str, bytes]) -> str:
    try:
        content = sources[path]
    except KeyError as exc:
        raise IsolatedSkillExecutorError(
            "skill_runtime_profile_unavailable",
            f"The exact entrypoint source is absent from the snapshot: {path}",
        ) from exc
    return content.decode("utf-8", errors="replace")


def _safe_source_candidate(
    candidate: PurePosixPath,
    sources: dict[str, bytes],
) -> str | None:
    if (
        candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        return None
    path = candidate.as_posix()
    return path if path in sources else None


def _module_candidates(
    bases: list[PurePosixPath],
    module_parts: list[str],
    sources: dict[str, bytes],
) -> set[str]:
    if (
        not module_parts
        or any(
            not part
            or part in {".", ".."}
            or "/" in part
            or "\\" in part
            for part in module_parts
        )
    ):
        return set()
    result: set[str] = set()
    for base in bases:
        stem = base.joinpath(*module_parts)
        for candidate in (
            stem.with_suffix(".py"),
            stem / "__init__.py",
        ):
            safe = _safe_source_candidate(candidate, sources)
            if safe is not None:
                result.add(safe)
    return result


def _python_dependencies(
    path: str,
    source: str,
    sources: dict[str, bytes],
) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    script = PurePosixPath(path)
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dependencies.update(_module_candidates(
                    [script.parent, PurePosixPath(".")],
                    alias.name.split("."),
                    sources,
                ))
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = script.parent
            for _ in range(max(0, node.level - 1)):
                base = base.parent
            bases = [base]
        else:
            bases = [script.parent, PurePosixPath(".")]
        module_parts = node.module.split(".") if node.module else []
        if module_parts:
            dependencies.update(_module_candidates(
                bases, module_parts, sources,
            ))
        for alias in node.names:
            if alias.name == "*":
                continue
            dependencies.update(_module_candidates(
                bases,
                [*module_parts, *alias.name.split(".")],
                sources,
            ))
    return dependencies


def _path_candidates_from_bases(
    value: str,
    bases: list[PurePosixPath],
    sources: dict[str, bytes],
) -> set[str]:
    if not value:
        return set()
    relative = PurePosixPath(value)
    result: set[str] = set()
    for base in bases:
        candidate = base / relative
        candidates: list[PurePosixPath] = []
        if candidate.suffix.casefold() in _SOURCE_SUFFIXES:
            candidates.append(candidate)
        elif not candidate.suffix:
            candidates.extend(
                candidate.with_suffix(suffix)
                for suffix in sorted(_SOURCE_SUFFIXES)
            )
            candidates.extend(
                candidate / f"index{suffix}"
                for suffix in (".js", ".mjs", ".cjs")
            )
        for item in candidates:
            safe = _safe_source_candidate(item, sources)
            if safe is not None:
                result.add(safe)
    return result


def _path_candidates(
    path: str,
    raw_path: str,
    sources: dict[str, bytes],
) -> set[str]:
    value = raw_path.strip()
    for prefix in _SHELL_SKILL_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):]
            bases = [PurePosixPath(".")]
            break
    else:
        if value.startswith(("/", "~")) or "\x00" in value:
            return set()
        bases = [PurePosixPath(path).parent, PurePosixPath(".")]
    return _path_candidates_from_bases(value, bases, sources)


def _dispatch_path_candidates(
    path: str,
    raw_path: str,
    sources: dict[str, bytes],
    *,
    runtime_script_base: PurePosixPath | None = None,
) -> tuple[set[str], str | None]:
    """Resolve a runtime filesystem dispatch and its required cwd policy."""

    value = raw_path.strip()
    for prefix in _SHELL_SKILL_PREFIXES:
        if value.startswith(prefix):
            local = _path_candidates_from_bases(
                value[len(prefix):],
                [PurePosixPath(".")],
                sources,
            )
            return local, None
    if value.startswith(("/", "~")) or "\x00" in value:
        return set(), None
    script_base = (
        runtime_script_base
        if runtime_script_base is not None
        else PurePosixPath(path).parent
    )
    script_local = _path_candidates_from_bases(
        value, [script_base], sources
    )
    skill_local = _path_candidates_from_bases(
        value, [PurePosixPath(".")], sources
    )
    if script_local and skill_local and script_local != skill_local:
        raise IsolatedSkillExecutorError(
            "skill_runtime_cwd_ambiguous",
            f"Local dispatch path {raw_path!r} resolves to different exact "
            "Skill files under script and skill cwd policies; anchor it with "
            "$CHATDS_SKILL_DIR.",
        )
    local = script_local or skill_local
    if not local:
        return set(), None
    if script_local and (
        not skill_local or script_base != PurePosixPath(".")
    ):
        return local, "script"
    return local, "skill"


def _python_module_specifier_analysis(
    path: str,
    specifier: str,
    sources: dict[str, bytes],
) -> tuple[set[str], set[str]]:
    """Resolve one literal Python module specifier without importing it."""

    value = str(specifier or "").strip()
    if not value or "\x00" in value:
        return set(), set()
    level = len(value) - len(value.lstrip("."))
    module = value[level:]
    parts = module.split(".") if module else []
    script = PurePosixPath(path)
    if level:
        base = script.parent
        for _ in range(max(0, level - 1)):
            base = base.parent
        bases = [base]
        roots: set[str] = set()
    else:
        bases = [script.parent, PurePosixPath(".")]
        roots = {parts[0]} if parts else set()
    return _module_candidates(bases, parts, sources), roots


def _literal_command_name(value: str) -> str | None:
    """Return a bounded executable basename suitable for capability preflight."""

    raw = str(value or "").strip()
    if not raw or "\x00" in raw or raw.startswith("-"):
        return None
    name = PurePosixPath(raw).name if "/" in raw else raw
    return name if _LITERAL_COMMAND_NAME_RE.fullmatch(name) else None


def _looks_local_source_reference(value: str) -> bool:
    raw = str(value or "").strip()
    return bool(
        raw
        and "://" not in raw
        and (
            raw.startswith(("./", "../", *_SHELL_SKILL_PREFIXES))
            or PurePosixPath(raw).suffix.casefold() in _SOURCE_SUFFIXES
        )
    )


def _direct_script_interpreter(
    source_path: str,
    sources: dict[str, bytes],
) -> str | None:
    """Return an approved shebang interpreter for direct local execution."""

    raw = sources.get(source_path, b"")
    first_line = raw.splitlines()[0][:256] if raw else b""
    if not first_line.startswith(b"#!"):
        return None
    try:
        tokens = shlex.split(
            first_line[2:].decode("utf-8", errors="strict"),
            posix=True,
        )
    except (UnicodeError, ValueError):
        return None
    if not tokens:
        return None
    launcher = tokens.pop(0)
    if launcher not in _FIXED_SHEBANG_LAUNCHERS:
        return None
    command = PurePosixPath(launcher).name.casefold()
    if command == "env":
        while tokens and tokens[0].startswith("-"):
            tokens.pop(0)
        if not tokens:
            return None
        command = PurePosixPath(tokens[0]).name.casefold()
    suffix = PurePosixPath(source_path).suffix.casefold()
    allowed = {
        ".py": _PYTHON_COMMAND_NAMES,
        ".js": _NODE_COMMAND_NAMES,
        ".mjs": _NODE_COMMAND_NAMES,
        ".cjs": _NODE_COMMAND_NAMES,
        ".sh": _SHELL_COMMAND_NAMES,
        ".bash": _SHELL_COMMAND_NAMES,
    }.get(suffix, frozenset())
    return command if command in allowed else None


def _validate_direct_local_scripts(
    paths: set[str],
    sources: dict[str, bytes],
) -> set[str]:
    commands: set[str] = set()
    for source_path in sorted(paths):
        interpreter = _direct_script_interpreter(
            source_path, sources
        )
        if interpreter is None:
            raise IsolatedSkillExecutorError(
                "skill_runtime_direct_entrypoint_unsupported",
                f"Direct local script {source_path!r} lacks a bounded "
                "suffix-compatible shebang; invoke it through an explicit "
                "python/node/bash interpreter instead.",
            )
        commands.add(interpreter)
    return commands


def _safe_shell_assignment(value: str) -> str | None:
    """Accept only inert scalar assignment bytes for local-path propagation."""

    raw = str(value)
    if (
        not raw
        or "\x00" in raw
        or any(char in raw for char in ("$", "`", "*", "?", "[", "]"))
    ):
        return None
    return raw


def _consume_shell_assignment_tail(
    value: str,
    tokens: list[str],
) -> str:
    """Consume shlex-split arithmetic/command-substitution assignment bytes."""

    raw = value
    if "$((" in raw and not raw.rstrip().endswith("))"):
        terminator = "))"
    elif "$(" in raw and not raw.rstrip().endswith(")"):
        terminator = ")"
    elif raw.count("`") % 2:
        terminator = "`"
    else:
        return raw
    while tokens:
        token = tokens.pop(0)
        raw += " " + token
        if (
            terminator == "`"
            and raw.count("`") % 2 == 0
        ) or (
            terminator != "`"
            and raw.rstrip().endswith(terminator)
        ):
            break
    return raw


def _resolve_shell_word(
    word: str,
    variables: dict[str, str],
) -> str | None:
    raw = str(word or "")
    if raw.startswith(_SHELL_SKILL_PREFIXES):
        remainder = next(
            raw[len(prefix):]
            for prefix in _SHELL_SKILL_PREFIXES
            if raw.startswith(prefix)
        )
        if "$" not in remainder and "`" not in remainder:
            return raw
    match = re.fullmatch(
        r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|"
        r"([A-Za-z_][A-Za-z0-9_]*))",
        raw,
    )
    if match is not None:
        return variables.get(match.group(1) or match.group(2))
    if "$" in raw or "`" in raw:
        return None
    return raw


def _shell_tokens_without_redirections(
    tokens: list[str],
) -> tuple[list[str], bool]:
    """Remove bounded shell redirections and report stdin redirection."""

    result: list[str] = []
    input_redirected = False
    index = 0
    operators = {
        "<", "<<", "<<<", "<<-", "<&", "<>", ">", ">>", ">&",
        ">|", "&>", "&>>",
    }
    while index < len(tokens):
        token = tokens[index]
        operator_index = index
        if (
            token.isdigit()
            and index + 1 < len(tokens)
            and tokens[index + 1] in operators
        ):
            operator_index += 1
        operator = tokens[operator_index]
        if operator in operators:
            input_redirected = input_redirected or operator.startswith("<")
            index = operator_index + 2
            continue
        result.append(token)
        index += 1
    return result, input_redirected


def _shell_invocation_tokens(segment: list[str]) -> tuple[str, list[str]] | None:
    """Return a literal unwrapped command and argv for one shell segment."""

    segment, _ = _shell_tokens_without_redirections(list(segment))
    while segment and (
        segment[0] in _SHELL_CONTROL_PREFIXES
        or re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*",
            segment[0],
        )
    ):
        segment.pop(0)
    while segment and segment[0] in {"command", "exec", "env"}:
        wrapper = segment.pop(0)
        while segment and (
            segment[0].startswith("-")
            or (
                wrapper == "env"
                and re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*=.*",
                    segment[0],
                )
            )
        ):
            segment.pop(0)
    if not segment:
        return None
    command = _literal_command_name(segment[0])
    if command is None:
        return None
    return command.casefold(), segment[1:]


def _interpreter_executes_stdin(
    command_key: str,
    arguments: list[str],
) -> bool:
    """Return whether a literal interpreter invocation executes stdin."""

    if command_key in _PYTHON_COMMAND_NAMES:
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument in {"-c", "--command", "-m", "--module"}:
                return False
            if argument == "--":
                return index + 1 >= len(arguments) or (
                    arguments[index + 1] == "-"
                )
            if argument in {"-W", "-X"}:
                index += 2
                continue
            if argument.startswith("-") and argument != "-":
                index += 1
                continue
            return argument == "-"
        return True
    if command_key in _NODE_COMMAND_NAMES:
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument in {"-e", "--eval", "-p", "--print"}:
                return False
            if argument == "--":
                return index + 1 >= len(arguments) or (
                    arguments[index + 1] == "-"
                )
            if argument in {"-r", "--require", "--loader", "--import"}:
                index += 2
                continue
            if argument.startswith("-") and argument != "-":
                index += 1
                continue
            return argument == "-"
        return True
    if command_key in _SHELL_COMMAND_NAMES:
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument == "--":
                return index + 1 >= len(arguments) or (
                    arguments[index + 1] == "-"
                )
            if argument == "-s" or argument.startswith("--stdin"):
                return True
            if (
                argument.startswith("-")
                and argument != "-"
            ):
                if "c" in argument[1:]:
                    return False
                index += 1
                continue
            return argument == "-"
        return True
    return False


def _heredoc_segment_requires_marker(
    segment: list[str],
    *,
    piped: bool,
) -> bool:
    """Classify one heredoc owner/downstream segment conservatively."""

    invocation = _shell_invocation_tokens(segment)
    if invocation is None:
        # A lone dynamic owner can be resolved by the statement analysis, but
        # a dynamic pipeline stage can transform data into executable input.
        return piped
    command_key, arguments = invocation
    if command_key in _SHELL_STDIN_DISPATCH_COMMANDS:
        return True
    if command_key in (
        _PYTHON_COMMAND_NAMES
        | _NODE_COMMAND_NAMES
        | _SHELL_COMMAND_NAMES
    ):
        return _interpreter_executes_stdin(command_key, arguments)
    if (
        piped
        and command_key not in _SHELL_BOUNDED_PIPE_STDIN_CONSUMERS
        and not _looks_local_source_reference(command_key)
    ):
        return True
    return False


def _heredoc_group_has_interpreter(
    line: str,
    operator_index: int,
) -> bool:
    """Detect an interpreter receiving this input in its pipeline group."""

    start = 0
    end = len(line)
    single_quoted = False
    double_quoted = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not single_quoted:
            escaped = True
            index += 1
            continue
        if char == "'" and not double_quoted:
            single_quoted = not single_quoted
            index += 1
            continue
        if char == '"' and not single_quoted:
            double_quoted = not double_quoted
            index += 1
            continue
        if single_quoted or double_quoted:
            index += 1
            continue
        if _shell_comment_starts(line, index):
            if index > operator_index:
                end = index
            break
        boundary_length = 0
        if char == "&" and (
            (
                index > 0
                and line[index - 1] in {"|", "<", ">"}
            )
            or (
                index + 1 < len(line)
                and line[index + 1] == ">"
            )
        ):
            index += 1
            continue
        if char in {";", "&"}:
            boundary_length = (
                2 if index + 1 < len(line)
                and line[index + 1] == char else 1
            )
        elif line.startswith("||", index):
            boundary_length = 2
        if boundary_length:
            if index < operator_index:
                start = index + boundary_length
            elif index > operator_index:
                end = index
                break
            index += boundary_length
            continue
        index += 1

    group = line[start:end]
    operator_segment = _shell_pipeline_segment_index(
        group, operator_index - start
    )
    try:
        lexer = shlex.shlex(
            group,
            posix=True,
            punctuation_chars="|&<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        return True
    segments: list[list[str]] = []
    segment: list[str] = []
    for token in [*tokens, "|"]:
        if token in {"|", "|&"}:
            segments.append(segment)
            segment = []
        else:
            segment.append(token)
    piped = len(segments) > 1
    for segment in segments[operator_segment:]:
        if segment and _heredoc_segment_requires_marker(
            segment, piped=piped
        ):
            return True
    return False


def _shell_pipeline_segment_index(source: str, stop: int) -> int:
    """Count pipeline boundaries before one input-redirection operator."""

    segment = 0
    single_quoted = False
    double_quoted = False
    escaped = False
    index = 0
    while index < min(stop, len(source)):
        char = source[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not single_quoted:
            escaped = True
            index += 1
            continue
        if char == "'" and not double_quoted:
            single_quoted = not single_quoted
            index += 1
            continue
        if char == '"' and not single_quoted:
            double_quoted = not double_quoted
            index += 1
            continue
        if single_quoted or double_quoted:
            index += 1
            continue
        if char == "|":
            if index + 1 < stop and source[index + 1] == "|":
                index += 2
                continue
            segment += 1
            if index + 1 < stop and source[index + 1] == "&":
                index += 2
                continue
        index += 1
    return segment


def _shell_comment_starts(source: str, index: int) -> bool:
    """Return whether ``#`` begins a shell comment at a token boundary."""

    return bool(
        source[index] == "#"
        and (
            index == 0
            or source[index - 1].isspace()
            or source[index - 1] in _SHELL_COMMENT_BOUNDARY_CHARS
        )
    )


def _shell_line_has_interpreter_here_string(line: str) -> bool:
    """Return true for a here-string feeding a Python/Node/shell command."""

    single_quoted = False
    double_quoted = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            if char == "<":
                while index < len(line) and line[index] == "<":
                    index += 1
            else:
                index += 1
            continue
        if char == "\\" and not single_quoted:
            escaped = True
            index += 1
            continue
        if char == "'" and not double_quoted:
            single_quoted = not single_quoted
            index += 1
            continue
        if char == '"' and not single_quoted:
            double_quoted = not double_quoted
            index += 1
            continue
        if single_quoted or double_quoted:
            index += 1
            continue
        if _shell_comment_starts(line, index):
            break
        if line.startswith("<<<", index):
            if _heredoc_group_has_interpreter(line, index):
                return True
            index += 3
            continue
        index += 1
    return False


def _shell_line_heredocs(
    line: str,
) -> tuple[_ShellHeredocDescriptor, ...]:
    """Parse heredoc operators outside shell quotes on one command line."""

    result: list[_ShellHeredocDescriptor] = []
    index = 0
    single_quoted = False
    double_quoted = False
    escaped = False
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            if char == "<":
                while index < len(line) and line[index] == "<":
                    index += 1
            else:
                index += 1
            continue
        if char == "\\" and not single_quoted:
            escaped = True
            index += 1
            continue
        if char == "'" and not double_quoted:
            single_quoted = not single_quoted
            index += 1
            continue
        if char == '"' and not single_quoted:
            double_quoted = not double_quoted
            index += 1
            continue
        if single_quoted or double_quoted:
            index += 1
            continue
        if _shell_comment_starts(line, index):
            break
        # Do not confuse arithmetic shift or here-string syntax with heredoc.
        if line.startswith("$((", index):
            closing = line.find("))", index + 3)
            if closing < 0:
                break
            index = closing + 2
            continue
        if line.startswith("((", index):
            closing = line.find("))", index + 2)
            if closing < 0:
                break
            index = closing + 2
            continue
        if char != "<":
            index += 1
            continue
        run_end = index
        while run_end < len(line) and line[run_end] == "<":
            run_end += 1
        run_length = run_end - index
        if run_length == 3:
            # Bash here-string; it has no following body/terminator.
            index = run_end
            continue
        if run_length > 3:
            raise IsolatedSkillExecutorError(
                "skill_runtime_heredoc_unsupported",
                "A shell input-redirection operator is outside the bounded "
                "heredoc/here-string grammar.",
            )
        if run_length != 2:
            index = run_end
            continue

        operator_index = index
        index += 2
        strip_tabs = index < len(line) and line[index] == "-"
        if strip_tabs:
            index += 1
        while index < len(line) and line[index] in {" ", "\t"}:
            index += 1
        if index >= len(line):
            raise IsolatedSkillExecutorError(
                "skill_runtime_heredoc_unsupported",
                "A shell heredoc operator has no bounded literal delimiter.",
            )

        delimiter: list[str] = []
        quoted = False
        while index < len(line):
            char = line[index]
            if char.isspace() or char in ";&|<>":
                break
            if char == "'":
                quoted = True
                closing = line.find("'", index + 1)
                if closing < 0:
                    raise IsolatedSkillExecutorError(
                        "skill_runtime_heredoc_unsupported",
                        "A shell heredoc delimiter has an unterminated quote.",
                    )
                delimiter.append(line[index + 1:closing])
                index = closing + 1
                continue
            if char == '"':
                quoted = True
                closing = index + 1
                value: list[str] = []
                while closing < len(line) and line[closing] != '"':
                    if (
                        line[closing] == "\\"
                        and closing + 1 < len(line)
                    ):
                        closing += 1
                    value.append(line[closing])
                    closing += 1
                if closing >= len(line):
                    raise IsolatedSkillExecutorError(
                        "skill_runtime_heredoc_unsupported",
                        "A shell heredoc delimiter has an unterminated quote.",
                    )
                delimiter.append("".join(value))
                index = closing + 1
                continue
            if char == "\\":
                quoted = True
                if index + 1 >= len(line):
                    raise IsolatedSkillExecutorError(
                        "skill_runtime_heredoc_unsupported",
                        "A shell heredoc delimiter ends in an escape.",
                    )
                delimiter.append(line[index + 1])
                index += 2
                continue
            delimiter.append(char)
            index += 1
        value = "".join(delimiter)
        if (
            not re.fullmatch(r"[A-Za-z0-9_.:+/@%-]{1,128}", value)
            or "$" in value
            or "`" in value
        ):
            raise IsolatedSkillExecutorError(
                "skill_runtime_heredoc_unsupported",
                "A shell heredoc delimiter is dynamic or outside the bounded "
                "literal delimiter grammar.",
            )
        result.append(_ShellHeredocDescriptor(
            delimiter=value,
            strip_tabs=strip_tabs,
            quoted=quoted,
            interpreter_input=_heredoc_group_has_interpreter(
                line, operator_index
            ),
        ))
        if len(result) > MAX_SHELL_HEREDOCS:
            raise IsolatedSkillExecutorError(
                "skill_runtime_heredoc_limit",
                "A shell source exceeds the bounded heredoc count.",
            )
    return tuple(result)


def _shell_logical_continuation_kind(content: str) -> str | None:
    """Return backslash/implicit continuation for one accumulated header."""

    single_quoted = False
    double_quoted = False
    escaped = False
    parenthesis_depth = 0
    effective_end = len(content)
    index = 0
    while index < len(content):
        char = content[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not single_quoted:
            escaped = True
            index += 1
            continue
        if char == "'" and not double_quoted:
            single_quoted = not single_quoted
        elif char == '"' and not single_quoted:
            double_quoted = not double_quoted
        elif not single_quoted and not double_quoted:
            if _shell_comment_starts(content, index):
                effective_end = index
                break
            if char == "(":
                parenthesis_depth += 1
            elif char == ")" and parenthesis_depth:
                parenthesis_depth -= 1
        index += 1

    effective = content[:effective_end]
    trailing_backslashes = (
        len(effective) - len(effective.rstrip("\\"))
    )
    if trailing_backslashes % 2 and not single_quoted:
        return "backslash"
    if single_quoted or double_quoted or parenthesis_depth:
        return "implicit"
    stripped = effective.rstrip()
    if stripped.endswith(("|&", "&&", "||", "|")):
        return "implicit"
    return None


def _literal_shell_arithmetic_end(source: str, index: int) -> int | None:
    """Return the end of a bounded, numeric-only ``$((...))`` expansion."""

    if not source.startswith("$((", index):
        return None
    closing = source.find("))", index + 3)
    if closing < 0:
        return None
    expression = source[index + 3:closing]
    if (
        not re.search(r"[0-9]", expression)
        or re.fullmatch(
            r"[0-9\s()+\-*/%<>&|^~!?:]+",
            expression,
        )
        is None
    ):
        return None
    return closing + 2


def _fold_shell_logical_line(
    lines: list[str],
    index: int,
) -> tuple[str, int]:
    logical = ""
    while index < len(lines):
        raw = lines[index]
        content = raw.rstrip("\r\n")
        logical += content
        continuation = _shell_logical_continuation_kind(logical)
        index += 1
        if continuation is not None:
            if index >= len(lines):
                raise IsolatedSkillExecutorError(
                    "skill_runtime_shell_parse_unsupported",
                    "A shell source ends in an unterminated logical-line "
                    "continuation.",
                )
            if (
                continuation == "implicit"
                and logical.rstrip().endswith(
                    ("|&", "&&", "||", "|")
                )
                and _shell_line_heredocs(logical)
            ):
                # Once a heredoc delimiter is complete, Bash consumes the
                # following physical line as body text. It does not complete
                # a dangling pipeline/boolean operator from that line.
                raise IsolatedSkillExecutorError(
                    "skill_runtime_shell_parse_unsupported",
                    "A heredoc header ends in an implicit shell operator "
                    "continuation that Bash would parse as body text.",
                )
            if continuation == "backslash":
                logical = logical[:-1]
            else:
                logical += " "
            continue
        return logical + (
            "\n" if raw.endswith(("\n", "\r")) else ""
        ), index
    return logical, index


def _unquoted_heredoc_body_has_execution(body: str) -> bool:
    """Detect executable expansion under Bash's unquoted-heredoc rules."""

    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            return True
        if (
            char == "$"
            and index + 1 < len(body)
            and body[index + 1] == "("
        ):
            if index + 2 < len(body) and body[index + 2] == "(":
                literal_end = _literal_shell_arithmetic_end(body, index)
                if literal_end is not None:
                    index = literal_end
                    continue
                # Non-literal shell arithmetic can recursively expand
                # variables and array subscripts.
                return True
            return True
        index += 1
    return False


def _preprocess_shell_heredocs(
    source: str,
) -> _ShellHeredocPreprocess:
    """Remove heredoc bodies while retaining executable header statements."""

    lines = source.splitlines(keepends=True)
    output: list[str] = []
    dynamic = False
    total_body_bytes = 0
    total_heredocs = 0
    index = 0
    while index < len(lines):
        header, index = _fold_shell_logical_line(lines, index)
        descriptors = _shell_line_heredocs(
            header.rstrip("\r\n")
        )
        if _shell_line_has_interpreter_here_string(
            header.rstrip("\r\n")
        ):
            dynamic = True
        output.append(header)
        if not descriptors:
            continue
        total_heredocs += len(descriptors)
        if total_heredocs > MAX_SHELL_HEREDOCS:
            raise IsolatedSkillExecutorError(
                "skill_runtime_heredoc_limit",
                "A shell source exceeds the bounded heredoc count.",
            )
        for descriptor in descriptors:
            body: list[str] = []
            terminated = False
            while index < len(lines):
                raw_line = lines[index]
                candidate = raw_line.rstrip("\r\n")
                if descriptor.strip_tabs:
                    candidate = candidate.lstrip("\t")
                index += 1
                if candidate == descriptor.delimiter:
                    terminated = True
                    break
                total_body_bytes += len(
                    raw_line.encode("utf-8", errors="replace")
                )
                if total_body_bytes > MAX_SHELL_HEREDOC_BODY_BYTES:
                    raise IsolatedSkillExecutorError(
                        "skill_runtime_heredoc_limit",
                        "Shell heredoc bodies exceed the bounded byte limit.",
                    )
                body.append(raw_line)
            if not terminated:
                raise IsolatedSkillExecutorError(
                    "skill_runtime_heredoc_unsupported",
                    "A shell heredoc is unterminated.",
                )
            if (
                descriptor.interpreter_input
                or (
                    not descriptor.quoted
                    and _unquoted_heredoc_body_has_execution(
                        "".join(body)
                    )
                )
            ):
                dynamic = True
    return _ShellHeredocPreprocess(
        source="".join(output),
        dynamic_dependency=dynamic,
    )


def _shell_statements(
    source: str,
) -> tuple[list[list[str]], bool]:
    """Tokenize bounded shell command statements without executing expansion."""

    result: list[list[str]] = []
    unparseable = False
    for line in source.splitlines():
        try:
            lexer = shlex.shlex(
                line,
                posix=True,
                punctuation_chars=";&|<>",
            )
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError:
            # Never silently drop a command-bearing line. An exact entrypoint
            # marker is required before an unparseable construct can run.
            unparseable = True
            continue
        statement: list[str] = []
        for token in tokens:
            if token in _SHELL_COMMAND_SEPARATORS:
                if statement:
                    result.append(statement)
                    statement = []
                continue
            statement.append(token)
        if statement:
            result.append(statement)
    return result, unparseable


def _shell_contains_command_substitution(source: str) -> bool:
    """Detect executable ``$(...)``/backticks outside single-quoted data."""

    single_quoted = False
    double_quoted = False
    escaped = False
    comment = False
    literal_arithmetic_until = 0
    for index, char in enumerate(source):
        if index < literal_arithmetic_until:
            continue
        if char == "\n":
            escaped = False
            comment = False
            continue
        if comment:
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\" and not single_quoted:
            escaped = True
            continue
        if char == "'" and not double_quoted:
            single_quoted = not single_quoted
            continue
        if char == '"' and not single_quoted:
            double_quoted = not double_quoted
            continue
        if single_quoted:
            continue
        if (
            not double_quoted
            and _shell_comment_starts(source, index)
        ):
            comment = True
            continue
        if char == "`":
            return True
        if char == "$" and index + 1 < len(source):
            if source[index + 1] == "(":
                if (
                    index + 2 < len(source)
                    and source[index + 2] == "("
                ):
                    literal_end = _literal_shell_arithmetic_end(
                        source, index
                    )
                    if literal_end is not None:
                        literal_arithmetic_until = literal_end
                        continue
                return True
        if (
            char in {"<", ">"}
            and index + 1 < len(source)
            and source[index + 1] == "("
        ):
            return True
    return False


def _shell_runtime_analysis(
    path: str,
    source: str,
    sources: dict[str, bytes],
    *,
    runtime_script_base: PurePosixPath | None = None,
) -> _DynamicSourceAnalysis:
    """Inspect only dependency-bearing shell positions.

    Variables used as ordinary URL/data arguments are intentionally ignored.
    A variable used as an executable, interpreter script/module, ``source``
    path, or eval payload is code authority and must be statically resolved or
    covered by an exact entrypoint runtime marker.
    """

    heredocs = _preprocess_shell_heredocs(source)
    source = heredocs.source
    dependencies: set[str] = set()
    python_roots: set[str] = set()
    node_packages: set[str] = set()
    commands: set[str] = set()
    required_cwds: set[str] = set()
    variables: dict[str, str] = {}
    dynamic = (
        heredocs.dynamic_dependency
        or _shell_contains_command_substitution(source)
    )
    cwd_mutated = False
    function_names = {
        match.group(1)
        for match in re.finditer(
            r"(?m)^\s*(?:function\s+)?"
            r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*\))?\s*\{",
            source,
        )
    }

    statements, unparseable_statements = _shell_statements(source)
    dynamic = dynamic or unparseable_statements
    for original in statements:
        tokens = list(original)
        while tokens:
            assignment = re.fullmatch(
                r"([A-Za-z_][A-Za-z0-9_]*)=(.*)",
                tokens[0],
            )
            if assignment is None:
                break
            tokens.pop(0)
            assignment_value = _consume_shell_assignment_tail(
                assignment.group(2), tokens
            )
            value = _safe_shell_assignment(assignment_value)
            if value is None:
                variables.pop(assignment.group(1), None)
            else:
                variables[assignment.group(1)] = value
        if not tokens:
            continue

        tokens, statement_input_redirected = (
            _shell_tokens_without_redirections(tokens)
        )
        if not tokens:
            continue

        # Declaration builtins may carry a list of simple assignments.
        if tokens[0] in {
            "declare", "export", "local", "readonly", "typeset",
        }:
            for token in tokens[1:]:
                if token.startswith("-"):
                    continue
                assignment = re.fullmatch(
                    r"([A-Za-z_][A-Za-z0-9_]*)=(.*)",
                    token,
                )
                if assignment is None:
                    continue
                value = _safe_shell_assignment(assignment.group(2))
                if value is None:
                    variables.pop(assignment.group(1), None)
                else:
                    variables[assignment.group(1)] = value
            continue

        # Remove leading redirections and shell control prefixes.  Compound
        # grammar bodies are still inspected statement-by-statement, while
        # reserved words never become bogus executor command requirements.
        while tokens and re.fullmatch(
            r"\d*(?:>>?|<<?|<>|>&|<&).*",
            tokens[0],
        ):
            redirection = tokens.pop(0)
            if (
                redirection.rstrip("&<>0123456789") == ""
                and tokens
            ):
                tokens.pop(0)
        while tokens and tokens[0] in _SHELL_CONTROL_PREFIXES:
            prefix = tokens.pop(0)
            if prefix == "time":
                while tokens and tokens[0].startswith("-"):
                    tokens.pop(0)
        is_function_definition = bool(
            tokens
            and (
                tokens[0] == "function"
                or tokens[0].removesuffix("()") in function_names
                and tokens[0].endswith("()")
            )
        )
        if is_function_definition:
            try:
                body_index = tokens.index("{") + 1
            except ValueError:
                continue
            tokens = tokens[body_index:]
        if tokens and tokens[0] == "coproc":
            # Optional coprocess names make the executable position ambiguous
            # without a full shell parser.
            dynamic = True
            continue
        if not tokens or tokens[0] in _SHELL_CONTROL_ONLY:
            continue
        # A case arm may put its first command after ``pattern)``.
        if tokens[0].endswith(")") and len(tokens) > 1:
            tokens.pop(0)
        if not tokens:
            continue
        # Unwrap shell builtins and ``env`` while retaining the real command
        # head for capability preflight.
        if (
            len(tokens) >= 2
            and tokens[0] == "command"
            and tokens[1] in {"-V", "-v"}
        ):
            continue
        while tokens and tokens[0] in {"command", "exec"}:
            tokens.pop(0)
            while tokens and tokens[0].startswith("-"):
                tokens.pop(0)
        if tokens and tokens[0] == "env":
            tokens.pop(0)
            while tokens and (
                tokens[0].startswith("-")
                or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*=.*",
                    tokens[0],
                )
            ):
                tokens.pop(0)
        if not tokens:
            continue

        raw_head = tokens[0]
        head = _resolve_shell_word(raw_head, variables)
        if head is None:
            dynamic = True
            continue
        command = _literal_command_name(head)
        command_key = (command or head).casefold()
        if (
            statement_input_redirected
            and command_key in (
                _PYTHON_COMMAND_NAMES
                | _NODE_COMMAND_NAMES
                | _SHELL_COMMAND_NAMES
            )
            and _interpreter_executes_stdin(
                command_key, tokens[1:]
            )
        ):
            dynamic = True

        if command_key in {".", "source"}:
            if len(tokens) < 2:
                dynamic = True
                continue
            target = _resolve_shell_word(tokens[1], variables)
            if target is None:
                dynamic = True
            else:
                local, required_cwd = _dispatch_path_candidates(
                    path,
                    target,
                    sources,
                    runtime_script_base=runtime_script_base,
                )
                if required_cwd is not None:
                    required_cwds.add(required_cwd)
                dependencies.update(local)
                if not local:
                    dynamic = True
            continue
        if command_key == "eval":
            # Even a quoted literal can synthesize expansion/dispatch after
            # this bounded pass; require an explicit entrypoint profile.
            dynamic = True
            continue
        if command_key in {"cd", "popd", "pushd"}:
            cwd_mutated = True
            continue

        direct_local: set[str] = set()
        if _looks_local_source_reference(head):
            direct_local, required_cwd = _dispatch_path_candidates(
                path,
                head,
                sources,
                runtime_script_base=runtime_script_base,
            )
            if required_cwd is not None:
                required_cwds.add(required_cwd)
            if direct_local:
                commands.update(
                    _validate_direct_local_scripts(
                        direct_local, sources
                    )
                )
            dependencies.update(direct_local)
            if not direct_local:
                dynamic = True

        if (
            command is not None
            and command_key not in _SHELL_BUILTINS
            and command not in function_names
            and not direct_local
        ):
            commands.add(command)

        arguments = tokens[1:]
        if command_key in _PYTHON_COMMAND_NAMES:
            index = 0
            while index < len(arguments):
                raw_argument = arguments[index]
                argument = _resolve_shell_word(raw_argument, variables)
                if argument in {"-c", "--command"}:
                    dynamic = True
                    break
                if argument in {"-m", "--module"}:
                    if index + 1 >= len(arguments):
                        dynamic = True
                        break
                    module = _resolve_shell_word(
                        arguments[index + 1], variables
                    )
                    if module is None:
                        dynamic = True
                    else:
                        local, roots = _python_module_specifier_analysis(
                            path, module, sources
                        )
                        dependencies.update(local)
                        python_roots.update(roots)
                    break
                if argument == "--":
                    index += 1
                    if index >= len(arguments):
                        break
                    argument = _resolve_shell_word(
                        arguments[index], variables
                    )
                elif argument is not None and argument.startswith("-"):
                    # -W and -X consume a non-code option value.
                    index += 2 if argument in {"-W", "-X"} else 1
                    continue
                if argument is None:
                    dynamic = True
                elif argument:
                    local, required_cwd = _dispatch_path_candidates(
                        path,
                        argument,
                        sources,
                        runtime_script_base=runtime_script_base,
                    )
                    if required_cwd is not None:
                        required_cwds.add(required_cwd)
                    dependencies.update(local)
                    if (
                        _looks_local_source_reference(argument)
                        and not local
                    ):
                        dynamic = True
                break
        elif command_key in _NODE_COMMAND_NAMES:
            index = 0
            while index < len(arguments):
                argument = _resolve_shell_word(
                    arguments[index], variables
                )
                if argument in {"-e", "--eval", "-p", "--print"}:
                    dynamic = True
                    break
                if argument in {"-r", "--require"}:
                    if index + 1 >= len(arguments):
                        dynamic = True
                        break
                    module = _resolve_shell_word(
                        arguments[index + 1], variables
                    )
                    if module is None:
                        dynamic = True
                    elif module.startswith("."):
                        local, required_cwd = _dispatch_path_candidates(
                            path,
                            module,
                            sources,
                            runtime_script_base=runtime_script_base,
                        )
                        dependencies.update(local)
                        if required_cwd is not None:
                            required_cwds.add(required_cwd)
                    else:
                        package = _canonical_node_package(module)
                        if package is not None:
                            node_packages.add(package)
                    index += 2
                    continue
                if argument == "--":
                    index += 1
                    if index >= len(arguments):
                        break
                    argument = _resolve_shell_word(
                        arguments[index], variables
                    )
                elif argument is not None and argument.startswith("-"):
                    index += 1
                    continue
                if argument is None:
                    dynamic = True
                elif argument:
                    local, required_cwd = _dispatch_path_candidates(
                        path,
                        argument,
                        sources,
                        runtime_script_base=runtime_script_base,
                    )
                    if required_cwd is not None:
                        required_cwds.add(required_cwd)
                    dependencies.update(local)
                    if (
                        _looks_local_source_reference(argument)
                        and not local
                    ):
                        dynamic = True
                break
        elif command_key in _SHELL_COMMAND_NAMES:
            index = 0
            while index < len(arguments):
                argument = _resolve_shell_word(
                    arguments[index], variables
                )
                if argument == "-c":
                    dynamic = True
                    break
                if argument == "--":
                    index += 1
                    if index >= len(arguments):
                        break
                    argument = _resolve_shell_word(
                        arguments[index], variables
                    )
                elif argument is not None and argument.startswith("-"):
                    index += 1
                    continue
                if argument is None:
                    dynamic = True
                elif argument:
                    local, required_cwd = _dispatch_path_candidates(
                        path,
                        argument,
                        sources,
                        runtime_script_base=runtime_script_base,
                    )
                    if required_cwd is not None:
                        required_cwds.add(required_cwd)
                    dependencies.update(local)
                    if (
                        _looks_local_source_reference(argument)
                        and not local
                    ):
                        dynamic = True
                break

    if cwd_mutated and required_cwds:
        raise IsolatedSkillExecutorError(
            "skill_runtime_cwd_mutation_unsupported",
            "A reachable shell source mutates cwd and also dispatches a bare "
            "relative local script. Anchor the dispatch with "
            "$CHATDS_SKILL_DIR.",
        )
    return _DynamicSourceAnalysis(
        local_dependencies=frozenset(dependencies),
        python_import_roots=frozenset(python_roots),
        node_packages=frozenset(node_packages),
        dynamic_dependency=dynamic,
        runtime_commands=frozenset(commands),
        required_cwds=frozenset(required_cwds),
        cwd_mutated=cwd_mutated,
    )


def _javascript_dependencies(
    path: str,
    source: str,
    sources: dict[str, bytes],
) -> set[str]:
    result: set[str] = set()
    for match in _JS_MODULE_RE.finditer(source):
        specifier = match.group(1)
        if specifier.startswith("."):
            result.update(_path_candidates(path, specifier, sources))
    return result


def _shell_dependencies(
    path: str,
    source: str,
    sources: dict[str, bytes],
) -> set[str]:
    return set(
        _shell_runtime_analysis(
            path, source, sources
        ).local_dependencies
    )


def _local_dependencies(
    path: str,
    source: str,
    sources: dict[str, bytes],
) -> set[str]:
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix == ".py":
        return _python_dependencies(path, source, sources)
    if suffix in {".js", ".mjs", ".cjs"}:
        return _javascript_dependencies(path, source, sources)
    if suffix in {".sh", ".bash"}:
        return _shell_dependencies(path, source, sources)
    return set()


def _python_import_roots(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _ast_literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _python_call_name(
    node: ast.AST,
    aliases: dict[str, str],
) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _python_call_name(node.value, aliases)
        return f"{owner}.{node.attr}" if owner else None
    return None


def _python_import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {
        "__import__": "__import__",
        "eval": "eval",
        "exec": "exec",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                aliases[bound] = (
                    alias.name
                    if alias.asname
                    else alias.name.split(".", 1)[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                aliases[alias.asname or alias.name] = (
                    f"{node.module}.{alias.name}"
                )
    return aliases


def _python_subprocess_analysis(
    path: str,
    call: ast.Call,
    sources: dict[str, bytes],
    *,
    runtime_script_base: PurePosixPath | None = None,
) -> _DynamicSourceAnalysis:
    """Inspect the executable and code-bearing positions of subprocess calls."""

    argument_node: ast.AST | None = (
        call.args[0] if call.args else None
    )
    if argument_node is None:
        for keyword in call.keywords:
            if keyword.arg == "args":
                argument_node = keyword.value
                break
    if argument_node is None:
        return _DynamicSourceAnalysis(dynamic_dependency=True)

    shell_mode = any(
        keyword.arg == "shell"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )
    literal_string = _ast_literal_string(argument_node)
    if literal_string is not None:
        if shell_mode:
            return _shell_runtime_analysis(
                path,
                literal_string,
                sources,
                runtime_script_base=runtime_script_base,
            )
        if _looks_local_source_reference(literal_string):
            local, required_cwd = _dispatch_path_candidates(
                path,
                literal_string,
                sources,
                runtime_script_base=runtime_script_base,
            )
            commands: set[str] = set()
            if local:
                commands.update(
                    _validate_direct_local_scripts(local, sources)
                )
            return _DynamicSourceAnalysis(
                local_dependencies=frozenset(local),
                dynamic_dependency=not local,
                runtime_commands=frozenset(commands),
                required_cwds=(
                    frozenset({required_cwd})
                    if required_cwd is not None
                    else frozenset()
                ),
            )
        command = _literal_command_name(literal_string)
        return _DynamicSourceAnalysis(
            dynamic_dependency=command is None,
            runtime_commands=(
                frozenset({command}) if command is not None
                else frozenset()
            ),
        )
    if not isinstance(argument_node, (ast.List, ast.Tuple)):
        return _DynamicSourceAnalysis(dynamic_dependency=True)
    elements = list(argument_node.elts)
    if not elements:
        return _DynamicSourceAnalysis(dynamic_dependency=True)
    head = _ast_literal_string(elements[0])
    command = _literal_command_name(head or "")
    if command is None:
        return _DynamicSourceAnalysis(dynamic_dependency=True)

    dependencies: set[str] = set()
    python_roots: set[str] = set()
    node_packages: set[str] = set()
    commands: set[str] = set()
    required_cwds: set[str] = set()
    dynamic = False
    command_key = command.casefold()
    arguments = elements[1:]
    looks_local_head = _looks_local_source_reference(head or "")
    if looks_local_head:
        local, required_cwd = _dispatch_path_candidates(
            path,
            head or "",
            sources,
            runtime_script_base=runtime_script_base,
        )
        if required_cwd is not None:
            required_cwds.add(required_cwd)
        if local:
            commands.update(
                _validate_direct_local_scripts(local, sources)
            )
        dependencies.update(local)
        if not local:
            dynamic = True

    if looks_local_head:
        pass
    elif command_key in _PYTHON_COMMAND_NAMES:
        index = 0
        while index < len(arguments):
            argument = _ast_literal_string(arguments[index])
            if argument in {"-c", "--command"}:
                dynamic = True
                break
            if argument in {"-m", "--module"}:
                if index + 1 >= len(arguments):
                    dynamic = True
                    break
                module = _ast_literal_string(arguments[index + 1])
                if module is None:
                    dynamic = True
                else:
                    local, roots = _python_module_specifier_analysis(
                        path, module, sources
                    )
                    dependencies.update(local)
                    python_roots.update(roots)
                break
            if argument == "--":
                index += 1
                if index >= len(arguments):
                    break
                argument = _ast_literal_string(arguments[index])
            elif argument is not None and argument.startswith("-"):
                index += 2 if argument in {"-W", "-X"} else 1
                continue
            if argument is None:
                dynamic = True
            elif argument:
                local, required_cwd = _dispatch_path_candidates(
                    path,
                    argument,
                    sources,
                    runtime_script_base=runtime_script_base,
                )
                if required_cwd is not None:
                    required_cwds.add(required_cwd)
                dependencies.update(local)
                if (
                    _looks_local_source_reference(argument)
                    and not local
                ):
                    dynamic = True
            break
    elif command_key in _NODE_COMMAND_NAMES:
        index = 0
        while index < len(arguments):
            argument = _ast_literal_string(arguments[index])
            if argument in {"-e", "--eval", "-p", "--print"}:
                dynamic = True
                break
            if argument in {"-r", "--require"}:
                if index + 1 >= len(arguments):
                    dynamic = True
                    break
                module = _ast_literal_string(arguments[index + 1])
                if module is None:
                    dynamic = True
                elif module.startswith("."):
                    local, required_cwd = _dispatch_path_candidates(
                        path,
                        module,
                        sources,
                        runtime_script_base=runtime_script_base,
                    )
                    dependencies.update(local)
                    if required_cwd is not None:
                        required_cwds.add(required_cwd)
                else:
                    package = _canonical_node_package(module)
                    if package is not None:
                        node_packages.add(package)
                index += 2
                continue
            if argument == "--":
                index += 1
                if index >= len(arguments):
                    break
                argument = _ast_literal_string(arguments[index])
            elif argument is not None and argument.startswith("-"):
                index += 1
                continue
            if argument is None:
                dynamic = True
            elif argument:
                local, required_cwd = _dispatch_path_candidates(
                    path,
                    argument,
                    sources,
                    runtime_script_base=runtime_script_base,
                )
                if required_cwd is not None:
                    required_cwds.add(required_cwd)
                dependencies.update(local)
                if (
                    _looks_local_source_reference(argument)
                    and not local
                ):
                    dynamic = True
            break
    elif command_key in _SHELL_COMMAND_NAMES:
        index = 0
        while index < len(arguments):
            argument = _ast_literal_string(arguments[index])
            if argument == "-c":
                dynamic = True
                break
            if argument == "--":
                index += 1
                if index >= len(arguments):
                    break
                argument = _ast_literal_string(arguments[index])
            elif argument is not None and argument.startswith("-"):
                index += 1
                continue
            if argument is None:
                dynamic = True
            elif argument:
                local, required_cwd = _dispatch_path_candidates(
                    path,
                    argument,
                    sources,
                    runtime_script_base=runtime_script_base,
                )
                if required_cwd is not None:
                    required_cwds.add(required_cwd)
                dependencies.update(local)
                if (
                    _looks_local_source_reference(argument)
                    and not local
                ):
                    dynamic = True
            break
    # Variables after a known non-interpreter executable are ordinary data
    # arguments (for example ``["curl", url]``), not code-loading authority.

    return _DynamicSourceAnalysis(
        local_dependencies=frozenset(dependencies),
        python_import_roots=frozenset(python_roots),
        node_packages=frozenset(node_packages),
        dynamic_dependency=dynamic,
        runtime_commands=frozenset(
            commands
            | (set() if looks_local_head else {command})
        ),
        required_cwds=frozenset(required_cwds),
    )


def _python_dynamic_runtime_analysis(
    path: str,
    source: str,
    sources: dict[str, bytes],
    *,
    runtime_script_base: PurePosixPath | None = None,
) -> _DynamicSourceAnalysis:
    """Find literal and non-literal Python code/dependency loading."""

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return _DynamicSourceAnalysis()
    aliases = _python_import_aliases(tree)
    dependencies: set[str] = set()
    python_roots: set[str] = set()
    node_packages: set[str] = set()
    commands: set[str] = set()
    required_cwds: set[str] = set()
    dynamic = False
    cwd_mutated = False
    dynamic_module_calls = {
        "__import__",
        "builtins.__import__",
        "importlib.import_module",
        "runpy.run_module",
    }
    run_path_calls = {"runpy.run_path"}
    subprocess_calls = {
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.run",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _python_call_name(node.func, aliases)
        if call_name in dynamic_module_calls:
            module = (
                _ast_literal_string(node.args[0])
                if node.args else None
            )
            if module is None:
                dynamic = True
            else:
                local, roots = _python_module_specifier_analysis(
                    path, module, sources
                )
                dependencies.update(local)
                python_roots.update(roots)
            continue
        if call_name in run_path_calls:
            target = (
                _ast_literal_string(node.args[0])
                if node.args else None
            )
            if target is None:
                dynamic = True
            else:
                local, required_cwd = _dispatch_path_candidates(
                    path,
                    target,
                    sources,
                    runtime_script_base=runtime_script_base,
                )
                if required_cwd is not None:
                    required_cwds.add(required_cwd)
                dependencies.update(local)
                if not local:
                    dynamic = True
            continue
        if call_name in subprocess_calls:
            analysis = _python_subprocess_analysis(
                path,
                node,
                sources,
                runtime_script_base=runtime_script_base,
            )
            dependencies.update(analysis.local_dependencies)
            python_roots.update(analysis.python_import_roots)
            node_packages.update(analysis.node_packages)
            commands.update(analysis.runtime_commands)
            required_cwds.update(analysis.required_cwds)
            dynamic = dynamic or analysis.dynamic_dependency
            continue
        if call_name in {"os.system"}:
            command_source = (
                _ast_literal_string(node.args[0])
                if node.args else None
            )
            if command_source is None:
                dynamic = True
            else:
                analysis = _shell_runtime_analysis(
                    path,
                    command_source,
                    sources,
                    runtime_script_base=runtime_script_base,
                )
                dependencies.update(analysis.local_dependencies)
                python_roots.update(analysis.python_import_roots)
                node_packages.update(analysis.node_packages)
                commands.update(analysis.runtime_commands)
                required_cwds.update(analysis.required_cwds)
                dynamic = dynamic or analysis.dynamic_dependency
            continue
        if call_name == "os.chdir":
            cwd_mutated = True
            continue
        if call_name in {"eval", "exec", "builtins.eval", "builtins.exec"}:
            dynamic = True

    if cwd_mutated and required_cwds:
        raise IsolatedSkillExecutorError(
            "skill_runtime_cwd_mutation_unsupported",
            "A reachable Python source mutates cwd and also dispatches a bare "
            "relative local script. Anchor the dispatch with an exact "
            "CHATDS_SKILL_DIR-derived path.",
        )
    return _DynamicSourceAnalysis(
        local_dependencies=frozenset(dependencies),
        python_import_roots=frozenset(python_roots),
        node_packages=frozenset(node_packages),
        dynamic_dependency=dynamic,
        runtime_commands=frozenset(commands),
        required_cwds=frozenset(required_cwds),
        cwd_mutated=cwd_mutated,
    )


def _source_browser_requirements(path: str, source: str) -> set[str]:
    marker = _RUNTIME_PROFILE_MARKER_RE.search(source)
    result: set[str] = set()
    if marker is not None:
        profile = marker.group(1)
        if profile not in SUPPORTED_RUNTIME_PROFILES:
            _unsupported_runtime_profile(profile)
        if profile == BROWSER_RUNTIME_PROFILE:
            result.add("__browser_profile__")
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix == ".py":
        imports = _python_import_roots(source)
        unsupported = sorted(
            imports.intersection(_UNSUPPORTED_BROWSER_PYTHON_IMPORTS)
        )
        if unsupported:
            _unsupported_browser_dependency(unsupported[0])
        result.update(imports.intersection(_BROWSER_PYTHON_IMPORTS))
        return result
    if suffix in {".js", ".mjs", ".cjs"}:
        modules = {
            (
                match.group(1).split("/", 2)[0]
                if not match.group(1).startswith("@")
                else "/".join(match.group(1).split("/", 2)[:2])
            )
            for match in _JS_MODULE_RE.finditer(source)
        }
        unsupported = sorted(
            modules.intersection(_UNSUPPORTED_BROWSER_NODE_PACKAGES)
        )
        if unsupported:
            _unsupported_browser_dependency(unsupported[0])
        if modules.intersection(_BROWSER_NODE_PACKAGES):
            result.add("__browser_node__")
    return result


def _source_runtime_profile_marker(source: str) -> str | None:
    marker = _RUNTIME_PROFILE_MARKER_RE.search(source)
    if marker is None:
        return None
    profile = marker.group(1)
    if profile not in SUPPORTED_RUNTIME_PROFILES:
        _unsupported_runtime_profile(profile)
    return profile


def _canonical_node_package(specifier: str) -> str | None:
    value = str(specifier or "").strip()
    if (
        not value
        or value.startswith((".", "/", "file:"))
        or "\x00" in value
    ):
        return None
    if value.startswith("node:"):
        return None
    root = value.split("/", 1)[0]
    if root in _NODE_BUILTINS:
        return None
    if value.startswith("@"):
        parts = value.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else value
    return root


def _source_node_packages(path: str, source: str) -> set[str]:
    if PurePosixPath(path).suffix.casefold() not in {
        ".js", ".mjs", ".cjs",
    }:
        return set()
    return {
        package
        for match in _JS_MODULE_RE.finditer(source)
        for package in (_canonical_node_package(match.group(1)),)
        if package is not None
    }


def _vendored_node_package_manifests(
    snapshot: SkillPackageSnapshot,
) -> dict[str, dict[str, Any]]:
    """Return root-vendored manifests with exact package content.

    Nested ``node_modules`` layouts can contain several versions of the same
    package and require Node's path-sensitive resolution algorithm.  The
    fixed Skill runtime therefore accepts only one root-vendored package
    identity per name; unsupported layouts fail closed as absent.
    """

    manifests: dict[str, dict[str, Any]] = {}
    for path in snapshot.paths:
        parts = PurePosixPath(path).parts
        if not parts or parts[-1] != "package.json":
            continue
        if (
            len(parts) == 3
            and parts[0] == "node_modules"
        ):
            package = parts[1]
            package_prefix = PurePosixPath("node_modules", package)
        elif (
            len(parts) == 4
            and parts[0] == "node_modules"
            and parts[1].startswith("@")
        ):
            package = f"{parts[1]}/{parts[2]}"
            package_prefix = PurePosixPath(
                "node_modules", parts[1], parts[2]
            )
        else:
            continue
        if package in manifests:
            continue
        raw = snapshot.read_bytes(path)
        if len(raw) > MAX_PACKAGE_JSON_BYTES:
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if str(value.get("name") or "") != package:
            continue
        package_files = [
            candidate
            for candidate in snapshot.paths
            if (
                PurePosixPath(candidate).is_relative_to(package_prefix)
                and candidate != path
            )
        ]
        if not package_files:
            continue
        manifests[package] = value
    return manifests


def _numeric_semver(value: Any) -> tuple[int, int, int] | None:
    match = _NUMERIC_SEMVER_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return tuple(int(match.group(index)) for index in range(1, 4))


def _node_version_satisfies(
    package: str,
    actual_version: Any,
    constraint: Any,
) -> bool:
    """Validate a bounded, common npm-semver subset without npm execution."""

    actual = _numeric_semver(actual_version)
    raw = str(constraint or "").strip()
    if actual is None:
        _unsupported_dependency_declaration(
            package,
            f"the attested/vendored version {actual_version!r} is not numeric semver",
        )
    if not raw or raw in {"*", "x", "X"}:
        return True
    exact = _numeric_semver(raw)
    if exact is not None:
        return actual == exact
    if "||" in raw or " - " in raw:
        _unsupported_dependency_declaration(
            package,
            f"npm range {raw!r} is outside the bounded semver subset",
        )

    if raw.startswith(("^", "~")):
        operator = raw[0]
        lower = _numeric_semver(raw[1:].strip())
        if lower is None:
            _unsupported_dependency_declaration(
                package,
                f"npm range {raw!r} is outside the bounded semver subset",
            )
        if operator == "~":
            upper = (lower[0], lower[1] + 1, 0)
        elif lower[0] > 0:
            upper = (lower[0] + 1, 0, 0)
        elif lower[1] > 0:
            upper = (0, lower[1] + 1, 0)
        else:
            upper = (0, 0, lower[2] + 1)
        return lower <= actual < upper

    wildcard = raw.casefold().replace("*", "x")
    wildcard_parts = wildcard.split(".")
    if (
        1 <= len(wildcard_parts) <= 3
        and "x" in wildcard_parts
        and all(
            part == "x" or part.isdigit()
            for part in wildcard_parts
        )
    ):
        for index, part in enumerate(wildcard_parts):
            if part != "x" and actual[index] != int(part):
                return False
        return True

    comparator_re = re.compile(
        r"^(>=|<=|>|<|=)?(v?[0-9]+\.[0-9]+\.[0-9]+)$"
    )
    tokens = raw.replace(",", " ").split()
    if not tokens:
        return True
    for token in tokens:
        match = comparator_re.fullmatch(token)
        if match is None:
            _unsupported_dependency_declaration(
                package,
                f"npm range {raw!r} is outside the bounded semver subset",
            )
        expected = _numeric_semver(match.group(2))
        operator = match.group(1) or "="
        if expected is None:
            return False
        comparison = {
            "=": actual == expected,
            ">": actual > expected,
            ">=": actual >= expected,
            "<": actual < expected,
            "<=": actual <= expected,
        }[operator]
        if not comparison:
            return False
    return True


def _validate_node_dependency_closure(
    snapshot: SkillPackageSnapshot,
    runtime_profile: str,
    required_packages: set[str],
    declared_constraints: dict[str, str],
) -> set[str]:
    fixed = set(_FIXED_NODE_PACKAGES.get(runtime_profile) or ())
    fixed_versions = dict(
        _FIXED_NODE_PACKAGE_VERSIONS.get(runtime_profile) or {}
    )
    vendored = _vendored_node_package_manifests(snapshot)
    pending = [
        (package, declared_constraints.get(package))
        for package in sorted(required_packages)
    ]
    checked: set[str] = set()
    while pending:
        package, constraint = pending.pop()
        if package in _UNSUPPORTED_BROWSER_NODE_PACKAGES:
            _unsupported_browser_dependency(package)
        if package in fixed:
            if (
                constraint is not None
                and not _node_version_satisfies(
                    package,
                    fixed_versions.get(package),
                    constraint,
                )
            ):
                _unsupported_dependency_declaration(
                    package,
                    f"declared npm range {constraint!r} excludes fixed "
                    f"version {fixed_versions.get(package)!r}",
                )
            checked.add(package)
            continue
        manifest = vendored.get(package)
        if not isinstance(manifest, dict):
            raise IsolatedSkillExecutorError(
                "skill_runtime_dependency_unsupported",
                f"Node package {package!r} is neither fixed in runtime profile "
                f"{runtime_profile!r} nor vendored in the exact Skill snapshot.",
            )
        version = manifest.get("version")
        if _numeric_semver(version) is None:
            _unsupported_dependency_declaration(
                package,
                "the vendored package manifest lacks a numeric semver version",
            )
        if (
            constraint is not None
            and not _node_version_satisfies(
                package,
                version,
                constraint,
            )
        ):
            _unsupported_dependency_declaration(
                package,
                f"declared npm range {constraint!r} excludes vendored "
                f"version {version!r}",
            )
        if package in checked:
            continue
        checked.add(package)
        dependencies = manifest.get("dependencies")
        if isinstance(dependencies, dict):
            pending.extend(
                (
                    str(name).casefold(),
                    str(version_constraint),
                )
                for name, version_constraint in dependencies.items()
                if str(name)
            )
    return checked


def _manifest_alias_value(
    value: dict[str, Any],
    *names: str,
) -> Any:
    """Read one aliased manifest field without accepting contradictions."""

    present = [(name, value[name]) for name in names if name in value]
    if not present:
        return None
    first = present[0][1]
    if any(candidate != first for _name, candidate in present[1:]):
        _invalid_runtime_manifest(
            "conflicting aliases were supplied for " + "/".join(names)
        )
    return first


def _manifest_entrypoint_path(
    snapshot: SkillPackageSnapshot,
    value: Any,
) -> str:
    raw = str(value or "").strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or len(raw) > 512
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw
        or path.suffix.casefold() not in _SOURCE_SUFFIXES
        or raw not in snapshot.paths
    ):
        _invalid_runtime_manifest(
            f"entrypoint {raw!r} is not an exact supported source in the "
            "package snapshot"
        )
    return raw


def _manifest_node_packages(
    runtime_profile: str,
    value: Any,
) -> tuple[tuple[str, str], ...]:
    if value in (None, {}, []):
        return ()
    fixed_versions = dict(
        _FIXED_NODE_PACKAGE_VERSIONS.get(runtime_profile) or {}
    )
    if isinstance(value, list):
        pairs: list[tuple[str, str]] = []
        for raw_name in value:
            if not isinstance(raw_name, str):
                _invalid_runtime_manifest(
                    "entrypoint Node dependency names must be strings"
                )
            name = str(raw_name or "").strip().casefold()
            version = fixed_versions.get(name)
            if not name or version is None:
                _unsupported_dependency_declaration(
                    name or "<empty>",
                    f"it is not fixed in runtime profile {runtime_profile!r}",
                )
            pairs.append((name, version))
    elif isinstance(value, dict):
        pairs = []
        for raw_name, raw_constraint in value.items():
            name = str(raw_name or "").strip().casefold()
            constraint = str(raw_constraint or "").strip()
            if (
                not name
                or _canonical_node_package(name) != name
                or _numeric_semver(constraint) is None
            ):
                _unsupported_dependency_declaration(
                    name or "<empty>",
                    "entrypoint manifests require an exact numeric Node "
                    "package version",
                )
            fixed = fixed_versions.get(name)
            if fixed is None:
                _unsupported_dependency_declaration(
                    name,
                    f"it is not fixed in runtime profile {runtime_profile!r}",
                )
            if fixed != constraint:
                _unsupported_dependency_declaration(
                    name,
                    f"manifest version {constraint!r} does not equal the "
                    f"fixed profile version {fixed!r}",
                )
            pairs.append((name, constraint))
    else:
        _invalid_runtime_manifest(
            "entrypoint Node dependencies must be an object or string list"
        )
    return tuple(sorted(dict(pairs).items()))


def _manifest_python_requirements(
    runtime_profile: str,
    value: Any,
) -> tuple[str, ...]:
    if value in (None, {}, []):
        return ()
    fixed_versions = {
        canonicalize_name(name): version
        for name, version in (
            _FIXED_PYTHON_PACKAGE_VERSIONS.get(runtime_profile) or {}
        ).items()
    }
    raw_requirements: list[str] = []
    if isinstance(value, dict):
        for raw_name, raw_version in value.items():
            if not isinstance(raw_version, str):
                _invalid_runtime_manifest(
                    "entrypoint Python dependency versions must be strings"
                )
            name = canonicalize_name(str(raw_name or "").strip())
            version = str(raw_version or "").strip()
            if not name or _numeric_semver(version) is None:
                _unsupported_dependency_declaration(
                    name or "<empty>",
                    "entrypoint manifests require an exact numeric Python "
                    "package version",
                )
            raw_requirements.append(f"{name}=={version}")
    elif isinstance(value, list):
        for raw_requirement in value:
            if not isinstance(raw_requirement, str):
                _invalid_runtime_manifest(
                    "entrypoint Python requirements must be strings"
                )
            requirement = str(raw_requirement or "").strip()
            normalized = canonicalize_name(requirement)
            if normalized in fixed_versions and requirement == normalized:
                requirement = (
                    f"{normalized}=={fixed_versions[normalized]}"
                )
            raw_requirements.append(requirement)
    else:
        _invalid_runtime_manifest(
            "entrypoint Python dependencies must be an object or string list"
        )

    result: list[str] = []
    for raw in raw_requirements:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            _unsupported_dependency_declaration(
                raw or "<empty>",
                "entrypoint manifests require valid exact PEP 508 "
                "requirements",
            )
        name = canonicalize_name(requirement.name)
        fixed = fixed_versions.get(name)
        specifiers = list(requirement.specifier)
        if (
            fixed is None
            or requirement.url is not None
            or requirement.marker is not None
            or len(specifiers) != 1
            or specifiers[0].operator not in {"==", "==="}
            or "*" in specifiers[0].version
            or specifiers[0].version != fixed
        ):
            _unsupported_dependency_declaration(
                name,
                f"the declaration must equal the package fixed in runtime "
                f"profile {runtime_profile!r}",
            )
        result.append(raw)
    return tuple(dict.fromkeys(result))


def _manifest_runtime_commands(value: Any) -> tuple[str, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list):
        _invalid_runtime_manifest(
            "entrypoint commands must be a string list"
        )
    commands: list[str] = []
    for raw_command in value:
        if not isinstance(raw_command, str):
            _invalid_runtime_manifest(
                "entrypoint commands must be strings"
            )
        raw = str(raw_command or "").strip()
        command = _literal_command_name(raw)
        if command is None or command != raw:
            _invalid_runtime_manifest(
                f"entrypoint command {raw!r} is not an exact executable name"
            )
        commands.append(command)
    return tuple(sorted(dict.fromkeys(commands)))


def _declared_entrypoint_runtime_profiles(
    snapshot: SkillPackageSnapshot,
) -> dict[str, _EntrypointRuntimeDeclaration]:
    """Parse the strict, package-root, snapshot-bound entrypoint manifest."""

    embedded_value: Any = None
    if "package.json" in snapshot.paths:
        package_raw = snapshot.read_bytes("package.json")
        if len(package_raw) <= MAX_PACKAGE_JSON_BYTES:
            try:
                package_value = json.loads(package_raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                package_value = None
            if isinstance(package_value, dict):
                embedded_value = package_value.get("chatdsRuntime")
    if (
        SKILL_RUNTIME_MANIFEST_NAME in snapshot.paths
        and embedded_value is not None
    ):
        _invalid_runtime_manifest(
            "declare either chatds-runtime.json or package.json "
            "chatdsRuntime, not both"
        )
    if SKILL_RUNTIME_MANIFEST_NAME in snapshot.paths:
        manifest_path = SKILL_RUNTIME_MANIFEST_NAME
        raw = snapshot.read_bytes(manifest_path)
        if len(raw) > MAX_SKILL_RUNTIME_MANIFEST_BYTES:
            _invalid_runtime_manifest("the manifest exceeds its byte limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            _invalid_runtime_manifest(
                "the manifest is not valid UTF-8 JSON"
            )
    elif embedded_value is not None:
        manifest_path = "package.json"
        raw = snapshot.read_bytes(manifest_path)
        value = embedded_value
    else:
        return {}
    if len(raw) > MAX_SKILL_RUNTIME_MANIFEST_BYTES:
        _invalid_runtime_manifest("the manifest exceeds its byte limit")
    if not isinstance(value, dict):
        _invalid_runtime_manifest("the top-level value must be an object")
    unknown_fields = set(value).difference({
        "schema_version", "schemaVersion", "entrypoints",
    })
    if unknown_fields:
        _invalid_runtime_manifest(
            "the top-level object has unsupported fields"
        )
    schema_version = _manifest_alias_value(
        value, "schema_version", "schemaVersion"
    )
    if type(schema_version) is not int or schema_version != 1:
        _invalid_runtime_manifest("schema_version must be integer 1")
    entries = value.get("entrypoints")
    if not isinstance(entries, (dict, list)):
        _invalid_runtime_manifest(
            "entrypoints must be an object or an ordered object list"
        )
    normalized_entries: list[dict[str, Any]] = []
    if isinstance(entries, dict):
        for path, record in entries.items():
            if not isinstance(record, dict):
                _invalid_runtime_manifest(
                    f"entrypoint {path!r} must map to an object"
                )
            if "path" in record or "entrypoint" in record:
                _invalid_runtime_manifest(
                    "object-form entrypoints must declare their path only "
                    "as the map key"
                )
            normalized_entries.append({"path": path, **record})
    else:
        for record in entries:
            if not isinstance(record, dict):
                _invalid_runtime_manifest(
                    "each entrypoint declaration must be an object"
                )
            normalized_entries.append(record)
    if (
        not normalized_entries
        or len(normalized_entries)
        > MAX_SKILL_RUNTIME_MANIFEST_ENTRYPOINTS
    ):
        _invalid_runtime_manifest(
            "entrypoints must contain between 1 and "
            f"{MAX_SKILL_RUNTIME_MANIFEST_ENTRYPOINTS} declarations"
        )

    manifest_sha256 = snapshot.file_sha256(
        manifest_path
    )
    result: dict[str, _EntrypointRuntimeDeclaration] = {}
    for record in normalized_entries:
        unknown_entrypoint_fields = set(record).difference({
            "path", "entrypoint",
            "runtime_profile", "runtimeProfile",
            "dependencies",
            "python_requirements", "pythonRequirements",
            "node_packages", "nodePackages",
            "runtime_commands", "runtimeCommands", "commands",
        })
        if unknown_entrypoint_fields:
            _invalid_runtime_manifest(
                "an entrypoint declaration has unsupported fields"
            )
        path = _manifest_entrypoint_path(
            snapshot,
            _manifest_alias_value(record, "path", "entrypoint"),
        )
        if path in result:
            _invalid_runtime_manifest(
                f"entrypoint {path!r} is declared more than once"
            )
        runtime_profile = _manifest_alias_value(
            record, "runtime_profile", "runtimeProfile"
        )
        if runtime_profile not in SUPPORTED_RUNTIME_PROFILES:
            _unsupported_runtime_profile(runtime_profile)
        dependencies = record.get("dependencies")
        if dependencies is None:
            dependencies = {}
        if not isinstance(dependencies, dict):
            _invalid_runtime_manifest(
                f"entrypoint {path!r} dependencies must be an object"
            )
        unknown_dependency_fields = set(dependencies).difference({
            "python", "node", "commands",
        })
        if unknown_dependency_fields:
            _invalid_runtime_manifest(
                f"entrypoint {path!r} has unsupported dependency fields"
            )
        python_value = _manifest_alias_value(
            record, "python_requirements", "pythonRequirements"
        )
        node_value = _manifest_alias_value(
            record, "node_packages", "nodePackages"
        )
        command_value = _manifest_alias_value(
            record,
            "runtime_commands",
            "runtimeCommands",
            "commands",
        )
        if python_value is not None and "python" in dependencies:
            _invalid_runtime_manifest(
                f"entrypoint {path!r} declares Python dependencies twice"
            )
        if node_value is not None and "node" in dependencies:
            _invalid_runtime_manifest(
                f"entrypoint {path!r} declares Node dependencies twice"
            )
        if command_value is not None and "commands" in dependencies:
            _invalid_runtime_manifest(
                f"entrypoint {path!r} declares commands twice"
            )
        result[path] = _EntrypointRuntimeDeclaration(
            runtime_profile=str(runtime_profile),
            python_requirements=_manifest_python_requirements(
                str(runtime_profile),
                (
                    python_value
                    if python_value is not None
                    else dependencies.get("python")
                ),
            ),
            node_packages=_manifest_node_packages(
                str(runtime_profile),
                (
                    node_value
                    if node_value is not None
                    else dependencies.get("node")
                ),
            ),
            runtime_commands=_manifest_runtime_commands(
                (
                    command_value
                    if command_value is not None
                    else dependencies.get("commands")
                )
            ),
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
        )
    return result


def _declared_package_profile(
    snapshot: SkillPackageSnapshot,
) -> tuple[
    str | None,
    dict[str, tuple[str, ...]],
    dict[str, str],
]:
    """Return package-wide machine declarations, never prose inference."""

    requirements: dict[str, list[str]] = {}
    node_packages: dict[str, str] = {}
    for path in snapshot.paths:
        if path.casefold() not in {
            "requirements.txt",
            "requirements.in",
        }:
            continue
        for raw_line in snapshot.read_bytes(path).decode(
            "utf-8", errors="replace"
        ).splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or line.startswith(("-", ".")):
                continue
            try:
                parsed = Requirement(line)
            except InvalidRequirement:
                raw_name = re.split(r"[\s<>=!~;\[]", line, 1)[0]
                normalized = canonicalize_name(raw_name.strip())
                if normalized in (
                    _BROWSER_REQUIREMENT_NAMES
                    | _UNSUPPORTED_BROWSER_REQUIREMENT_NAMES
                ):
                    _unsupported_dependency_declaration(
                        normalized,
                        f"invalid PEP 508 requirement {line!r}",
                    )
                continue
            normalized = canonicalize_name(parsed.name)
            requirements.setdefault(normalized, []).append(line)

    package_profile: str | None = None
    if "package.json" in snapshot.paths:
        raw = snapshot.read_bytes("package.json")
        if len(raw) <= MAX_PACKAGE_JSON_BYTES:
            try:
                package_data = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                package_data = None
            if isinstance(package_data, dict):
                declared = package_data.get("chatdsRuntimeProfile")
                if declared is not None:
                    if declared not in SUPPORTED_RUNTIME_PROFILES:
                        _unsupported_runtime_profile(declared)
                    package_profile = str(declared)
                for section_name in (
                    "dependencies",
                    "optionalDependencies",
                    "peerDependencies",
                ):
                    section = package_data.get(section_name)
                    if not isinstance(section, dict):
                        continue
                    for name, constraint in section.items():
                        normalized = str(name).casefold()
                        if normalized:
                            node_packages.setdefault(
                                normalized,
                                str(constraint),
                            )

    try:
        frontmatter, _body = parse_frontmatter(
            snapshot.read_bytes("SKILL.md").decode(
                "utf-8", errors="replace"
            )
        )
    except (KeyError, ValueError):
        frontmatter = {}
    if isinstance(frontmatter, dict):
        metadata = frontmatter.get("metadata")
        values = [
            frontmatter.get("runtime_profile"),
            frontmatter.get("runtime-profile"),
            (
                metadata.get("runtime_profile")
                if isinstance(metadata, dict) else None
            ),
            (
                metadata.get("runtime-profile")
                if isinstance(metadata, dict) else None
            ),
        ]
        declared_values = [value for value in values if value is not None]
        for declared in declared_values:
            if declared not in SUPPORTED_RUNTIME_PROFILES:
                _unsupported_runtime_profile(declared)
        if BROWSER_RUNTIME_PROFILE in declared_values:
            package_profile = BROWSER_RUNTIME_PROFILE
        elif BASE_RUNTIME_PROFILE in declared_values and package_profile is None:
            package_profile = BASE_RUNTIME_PROFILE
    return (
        package_profile,
        {
            name: tuple(dict.fromkeys(values))
            for name, values in requirements.items()
        },
        node_packages,
    )


def select_skill_runtime_profile(
    snapshot: SkillPackageSnapshot,
    entrypoint: str,
) -> SkillRuntimeSelection:
    """Select from one exact entrypoint and its bounded local source closure."""

    sources = _snapshot_sources(snapshot)
    if entrypoint not in sources:
        raise IsolatedSkillExecutorError(
            "missing_entrypoint",
            "The exact entrypoint is absent or is not a supported source file.",
        )
    (
        package_profile,
        package_requirements,
        declared_node_constraints,
    ) = _declared_package_profile(snapshot)
    entrypoint_declarations = _declared_entrypoint_runtime_profiles(
        snapshot
    )
    entrypoint_declaration = entrypoint_declarations.get(entrypoint)
    effective_package_requirements = {
        name: list(values)
        for name, values in package_requirements.items()
    }
    effective_node_constraints = dict(declared_node_constraints)
    if entrypoint_declaration is not None:
        for raw_requirement in (
            entrypoint_declaration.python_requirements
        ):
            parsed = Requirement(raw_requirement)
            name = canonicalize_name(parsed.name)
            effective_package_requirements.setdefault(name, []).append(
                raw_requirement
            )
        for name, constraint in entrypoint_declaration.node_packages:
            package_constraint = effective_node_constraints.get(name)
            if (
                package_constraint is not None
                and not _node_version_satisfies(
                    name,
                    constraint,
                    package_constraint,
                )
            ):
                _unsupported_dependency_declaration(
                    name,
                    f"package range {package_constraint!r} conflicts with "
                    f"entrypoint manifest version {constraint!r}",
                )
            effective_node_constraints[name] = constraint
    queue = [entrypoint]
    seen: set[str] = set()
    browser_requirements: set[str] = set()
    python_import_roots: set[str] = set()
    source_node_packages: set[str] = set()
    source_runtime_commands: set[str] = set()
    required_cwds: set[str] = set()
    cwd_mutated = False
    dynamic_dependency_sources: set[str] = set()
    dynamic_node_imports: set[str] = set()
    dynamic_python_or_shell = False
    inspected_bytes = 0
    runtime_script_base = PurePosixPath(entrypoint).parent
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > MAX_PROFILE_SOURCE_FILES:
            raise IsolatedSkillExecutorError(
                "skill_runtime_profile_limit",
                "The reachable Skill source closure exceeds its file limit.",
            )
        source = _decode_source(path, sources)
        inspected_bytes += len(sources[path])
        if inspected_bytes > MAX_PROFILE_SOURCE_BYTES:
            raise IsolatedSkillExecutorError(
                "skill_runtime_profile_limit",
                "The reachable Skill source closure exceeds its byte limit.",
            )
        browser_requirements.update(
            _source_browser_requirements(path, source)
        )
        source_node_packages.update(_source_node_packages(path, source))
        suffix = PurePosixPath(path).suffix.casefold()
        if suffix == ".py":
            local_dependencies = _python_dependencies(
                path, source, sources
            )
            static_roots = _python_import_roots(source)
            python_import_roots.update(static_roots)
            analysis = _python_dynamic_runtime_analysis(
                path,
                source,
                sources,
                runtime_script_base=runtime_script_base,
            )
            local_dependencies.update(analysis.local_dependencies)
            python_import_roots.update(analysis.python_import_roots)
            source_node_packages.update(analysis.node_packages)
            source_runtime_commands.update(
                analysis.runtime_commands
            )
            required_cwds.update(analysis.required_cwds)
            cwd_mutated = cwd_mutated or analysis.cwd_mutated
            if analysis.dynamic_dependency:
                dynamic_dependency_sources.add(path)
                dynamic_python_or_shell = True
        elif suffix in {".sh", ".bash"}:
            local_dependencies = set()
            analysis = _shell_runtime_analysis(
                path,
                source,
                sources,
                runtime_script_base=runtime_script_base,
            )
            local_dependencies.update(analysis.local_dependencies)
            python_import_roots.update(analysis.python_import_roots)
            source_node_packages.update(analysis.node_packages)
            source_runtime_commands.update(
                analysis.runtime_commands
            )
            required_cwds.update(analysis.required_cwds)
            cwd_mutated = cwd_mutated or analysis.cwd_mutated
            if analysis.dynamic_dependency:
                dynamic_dependency_sources.add(path)
                dynamic_python_or_shell = True
        else:
            local_dependencies = _local_dependencies(
                path, source, sources
            )
            if suffix in {".js", ".mjs", ".cjs"}:
                if _JS_DYNAMIC_MODULE_CALL_RE.search(source):
                    dynamic_node_imports.add(path)
                    dynamic_dependency_sources.add(path)
                if _JS_DYNAMIC_EXECUTION_RE.search(source):
                    dynamic_dependency_sources.add(path)
                    dynamic_python_or_shell = True
                if _JS_CWD_MUTATION_RE.search(source):
                    cwd_mutated = True
        queue.extend(
            sorted(
                local_dependencies - seen,
                reverse=True,
            )
        )

    has_node_source = any(
        PurePosixPath(path).suffix.casefold()
        in {".js", ".mjs", ".cjs"}
        for path in seen
    )
    browser_requirements.update(
        python_import_roots.intersection(
            _BROWSER_REQUIREMENT_NAMES
            | _UNSUPPORTED_BROWSER_REQUIREMENT_NAMES
        )
    )
    unsupported_node_packages = sorted(
        source_node_packages.intersection(
            _UNSUPPORTED_BROWSER_NODE_PACKAGES
        )
    )
    if unsupported_node_packages:
        _unsupported_browser_dependency(
            unsupported_node_packages[0]
        )
    if source_node_packages.intersection(_BROWSER_NODE_PACKAGES):
        browser_requirements.add("__browser_node__")
    python_browser_imports = python_import_roots.intersection(
        _BROWSER_REQUIREMENT_NAMES
        | _UNSUPPORTED_BROWSER_REQUIREMENT_NAMES
    )
    unsupported_requirements = sorted(
        python_browser_imports.intersection(
            _UNSUPPORTED_BROWSER_REQUIREMENT_NAMES
        )
    )
    if unsupported_requirements:
        _unsupported_browser_dependency(
            unsupported_requirements[0]
        )
    entrypoint_marker = _source_runtime_profile_marker(
        _decode_source(entrypoint, sources)
    )
    manifest_profile = (
        entrypoint_declaration.runtime_profile
        if entrypoint_declaration is not None else None
    )
    if (
        entrypoint_marker is not None
        and manifest_profile is not None
        and entrypoint_marker != manifest_profile
    ):
        _runtime_profile_conflict(
            "the entrypoint source marker and package manifest disagree"
        )
    if (
        package_profile is not None
        and manifest_profile is not None
        and package_profile != manifest_profile
    ):
        _runtime_profile_conflict(
            "the package-wide profile and exact entrypoint manifest disagree"
        )
    exact_entrypoint_profile = manifest_profile or entrypoint_marker
    if len(required_cwds) > 1:
        raise IsolatedSkillExecutorError(
            "skill_runtime_cwd_ambiguous",
            "The reachable Skill source closure dispatches local files under "
            "conflicting script/skill cwd policies. Anchor those paths with "
            "$CHATDS_SKILL_DIR.",
        )
    if cwd_mutated and required_cwds:
        raise IsolatedSkillExecutorError(
            "skill_runtime_cwd_mutation_unsupported",
            "The reachable Skill closure mutates cwd and dispatches a bare "
            "relative local script. Anchor dispatches with "
            "$CHATDS_SKILL_DIR.",
        )
    required_cwd = (
        next(iter(required_cwds)) if required_cwds else None
    )
    if (
        dynamic_dependency_sources
        and exact_entrypoint_profile is None
    ):
        raise IsolatedSkillExecutorError(
            "skill_runtime_dynamic_dependency_unsupported",
            "A reachable source uses non-literal dependency or code dispatch. "
            "Add an exact CHATDS_RUNTIME_PROFILE marker or a strict "
            f"{SKILL_RUNTIME_MANIFEST_NAME} entrypoint declaration, and "
            "declare fixed external dependencies in machine-readable "
            "manifests.",
        )
    if (
        dynamic_dependency_sources
        and entrypoint_declaration is not None
        and not (
            entrypoint_declaration.python_requirements
            or entrypoint_declaration.node_packages
            or entrypoint_declaration.runtime_commands
        )
    ):
        raise IsolatedSkillExecutorError(
            "skill_runtime_dynamic_dependency_unsupported",
            "A dynamic entrypoint manifest must declare at least one exact "
            "fixed dependency or runtime command.",
        )
    if (
        dynamic_node_imports
        and entrypoint_declaration is not None
        and not entrypoint_declaration.node_packages
    ):
        raise IsolatedSkillExecutorError(
            "skill_runtime_dynamic_dependency_unsupported",
            "A dynamic Node import requires at least one exact fixed Node "
            "dependency in its entrypoint manifest.",
        )
    if dynamic_node_imports or dynamic_python_or_shell:
        # The explicit entrypoint marker selects the profile. In the absence
        # of a statically knowable specifier, every declared root dependency
        # becomes part of the exact dependency proof.
        source_node_packages.update(effective_node_constraints)
    if entrypoint_declaration is not None:
        source_node_packages.update(
            name for name, _constraint
            in entrypoint_declaration.node_packages
        )
        source_runtime_commands.update(
            entrypoint_declaration.runtime_commands
        )
    inferred_profile = (
        BROWSER_RUNTIME_PROFILE
        if browser_requirements else BASE_RUNTIME_PROFILE
    )
    if (
        exact_entrypoint_profile == BASE_RUNTIME_PROFILE
        and inferred_profile == BROWSER_RUNTIME_PROFILE
    ):
        _runtime_profile_conflict(
            "the exact entrypoint declares base-v1 but its reachable "
            "dependency closure requires browser-automation-v1"
        )
    runtime_profile = (
        exact_entrypoint_profile
        or (
            BROWSER_RUNTIME_PROFILE
            if package_profile == BROWSER_RUNTIME_PROFILE
            else inferred_profile
        )
    )
    runtime_node_packages = (
        _validate_node_dependency_closure(
            snapshot,
            runtime_profile,
            source_node_packages,
            effective_node_constraints,
        )
        if has_node_source or source_node_packages else set()
    )
    suffix = PurePosixPath(entrypoint).suffix.casefold()
    command_set = {
        "python"
        for path in seen
        if PurePosixPath(path).suffix.casefold() == ".py"
    }
    command_set.update(
        "node"
        for path in seen
        if PurePosixPath(path).suffix.casefold()
        in {".js", ".mjs", ".cjs"}
    )
    if suffix in {".sh", ".bash"}:
        command_set.add("bash")
    command_set.update(source_runtime_commands)
    commands = tuple(sorted(command_set))
    runtime_requirements_set: set[str] = set()
    reachable_requirement_names: set[str] = set()
    for import_root in python_import_roots:
        normalized_root = canonicalize_name(import_root)
        requirement_name = _PYTHON_IMPORT_REQUIREMENT_ALIASES.get(
            normalized_root,
            normalized_root,
        )
        if requirement_name in effective_package_requirements:
            reachable_requirement_names.add(requirement_name)
    if dynamic_python_or_shell:
        reachable_requirement_names.update(
            effective_package_requirements
        )
    if entrypoint_declaration is not None:
        reachable_requirement_names.update(
            canonicalize_name(Requirement(raw).name)
            for raw in entrypoint_declaration.python_requirements
        )
    for requirement_name in sorted(reachable_requirement_names):
        if requirement_name in _UNSUPPORTED_BROWSER_REQUIREMENT_NAMES:
            _unsupported_browser_dependency(requirement_name)
        declared = (
            effective_package_requirements.get(requirement_name) or ()
        )
        if declared:
            runtime_requirements_set.update(declared)
    for requirement_name in sorted(python_browser_imports):
        if not effective_package_requirements.get(requirement_name):
            runtime_requirements_set.add(requirement_name)
    runtime_requirements = tuple(sorted(runtime_requirements_set))
    return SkillRuntimeSelection(
        runtime_profile=runtime_profile,
        package_sha256=snapshot.sha256,
        entrypoint=entrypoint,
        script_sha256=snapshot.file_sha256(entrypoint),
        reachable_sources=tuple(sorted(seen)),
        runtime_requirements=runtime_requirements,
        runtime_commands=commands,
        runtime_node_packages=tuple(sorted(runtime_node_packages)),
        required_cwd=required_cwd,
        runtime_manifest_path=(
            entrypoint_declaration.manifest_path
            if entrypoint_declaration is not None else None
        ),
        runtime_manifest_sha256=(
            entrypoint_declaration.manifest_sha256
            if entrypoint_declaration is not None else None
        ),
    )


def compile_skill_runtime_profile_manifest(
    skill_root: str | os.PathLike[str],
    entrypoints: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Compile serializable profile identities from one package snapshot."""

    from pathlib import Path
    from tools.isolated_skill_executor import snapshot_skill_package

    try:
        snapshot = snapshot_skill_package(Path(skill_root))
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "schema_version": 1,
            "valid": False,
            "error_code": str(
                getattr(exc, "code", None)
                or "invalid_skill_snapshot"
            ),
            "scripts": [],
        }
    try:
        declared_entrypoints = _declared_entrypoint_runtime_profiles(
            snapshot
        )
    except (RuntimeError, ValueError) as exc:
        return {
            "schema_version": 1,
            "valid": False,
            "package_sha256": snapshot.sha256,
            "error_code": str(
                getattr(exc, "code", None)
                or "skill_runtime_manifest_invalid"
            ),
            "scripts": [],
            "errors": [{
                "entrypoint": "__manifest__",
                "error_code": str(
                    getattr(exc, "code", None)
                    or "skill_runtime_manifest_invalid"
                ),
            }],
        }
    scripts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for entrypoint in dict.fromkeys(
        [
            *(str(item) for item in entrypoints if str(item)),
            *declared_entrypoints,
        ]
    ):
        try:
            selection = select_skill_runtime_profile(
                snapshot,
                entrypoint,
            )
        except (RuntimeError, ValueError) as exc:
            errors.append({
                "entrypoint": entrypoint,
                "error_code": str(
                    getattr(exc, "code", None)
                    or "skill_runtime_profile_unavailable"
                ),
            })
            continue
        scripts.append({
            "entrypoint": selection.entrypoint,
            "runtime_profile": selection.runtime_profile,
            "package_sha256": selection.package_sha256,
            "script_sha256": selection.script_sha256,
            "reachable_sources": list(selection.reachable_sources),
            "runtime_requirements": list(
                selection.runtime_requirements
            ),
            "runtime_commands": list(selection.runtime_commands),
            "runtime_node_packages": list(
                selection.runtime_node_packages
            ),
            "required_cwd": selection.required_cwd,
            "manifest_declared": (
                entrypoint in declared_entrypoints
            ),
            "runtime_manifest_path": (
                selection.runtime_manifest_path
            ),
            "runtime_manifest_sha256": (
                selection.runtime_manifest_sha256
            ),
        })
    result = {
        "schema_version": 1,
        "valid": not errors,
        "package_sha256": snapshot.sha256,
        "scripts": scripts,
        "errors": errors,
    }
    if declared_entrypoints:
        declaration = next(iter(declared_entrypoints.values()))
        result["entrypoint_manifest"] = {
            "path": declaration.manifest_path,
            "sha256": declaration.manifest_sha256,
            "schema_version": 1,
        }
    return result
