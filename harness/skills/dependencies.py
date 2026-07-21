"""Generic Python dependency discovery for installed skills."""

from __future__ import annotations

import ast
import configparser
import json
import re
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from skills.loader import parse_frontmatter

MANIFEST_NAMES = {
    "pyproject.toml",
    "setup.cfg",
    "Pipfile",
    "environment.yml",
    "environment.yaml",
}
RUNTIME_REQUIREMENTS_NAME = "requirements.txt"
PYTHON_EXTENSIONS = {".py"}
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "site-packages",
    "__pycache__",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
BUILTIN_PACKAGES = {
    "matplotlib",
    "networkx",
    "numpy",
    "openpyxl",
    "pandas",
    "PIL",
    "scipy",
    "sklearn",
    "sympy",
    "xlsxwriter",
}
IMPORT_PACKAGE_HINTS = {
    "Bio": "biopython",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
}
HIGH_CONFIDENCE_IMPORT_PACKAGES = {
    "aiohttp",
    "biopython",
    "beautifulsoup4",
    "httpx",
    "lxml",
    "openai",
    "opencv-python",
    "plotly",
    "pydantic",
    "pyyaml",
    "requests",
    "rich",
    "tenacity",
    "tqdm",
}
REQ_OPTION_RE = re.compile(r"^\s*(?:-|#)")
REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")


def scan_skill_dependencies(skill_dir: str | Path) -> dict[str, Any]:
    root = Path(skill_dir).resolve()
    if not root.is_dir():
        return {
            "python_packages": [],
            "entrypoints": [],
            "unsupported": [],
            "warnings": [f"Skill directory not found: {skill_dir}"],
            "sources": [],
            "heuristic_imports": [],
        }

    packages: list[str] = []
    entrypoints: list[str] = []
    unsupported: list[str] = []
    warnings: list[str] = []
    sources: list[dict[str, str]] = []

    _scan_frontmatter(root, packages, entrypoints, unsupported, warnings, sources)
    _scan_compiled_activation_contract(
        root,
        packages,
        unsupported,
        warnings,
        sources,
    )
    _scan_manifests(root, packages, unsupported, warnings, sources)
    _scan_mcp_entrypoints(root, entrypoints, warnings, sources)
    heuristic_imports = _scan_python_imports(root, packages, warnings)
    for package in heuristic_imports:
        if package in HIGH_CONFIDENCE_IMPORT_PACKAGES:
            packages.append(package)

    return {
        "python_packages": _dedupe(packages),
        "entrypoints": _dedupe(entrypoints),
        "unsupported": _dedupe(unsupported),
        "warnings": _dedupe(warnings),
        "sources": sources,
        "heuristic_imports": heuristic_imports,
    }


def scan_declared_skill_dependencies(skill_dir: str | Path) -> dict[str, Any]:
    """Scan only package-wide, machine-declared Skill dependencies.

    Public script runners use this lightweight report immediately before an
    isolated execution. Route-specific worker/bootstrap/aggregation contracts
    are already checked by activation preflight; re-compiling all nested route
    requirements here would incorrectly let an unrelated route block the
    selected entrypoint. Import heuristics are intentionally excluded because
    they are observations, not declarative installation requirements.
    """

    root = Path(skill_dir).resolve()
    if not root.is_dir():
        return {
            "python_packages": [],
            "entrypoints": [],
            "unsupported": [],
            "warnings": [f"Skill directory not found: {skill_dir}"],
            "sources": [],
            "heuristic_imports": [],
        }
    packages: list[str] = []
    entrypoints: list[str] = []
    unsupported: list[str] = []
    warnings: list[str] = []
    sources: list[dict[str, str]] = []
    _scan_frontmatter(
        root,
        packages,
        entrypoints,
        unsupported,
        warnings,
        sources,
    )
    _scan_manifests(root, packages, unsupported, warnings, sources)
    return {
        "python_packages": _dedupe(packages),
        "entrypoints": _dedupe(entrypoints),
        "unsupported": _dedupe(unsupported),
        "warnings": _dedupe(warnings),
        "sources": sources,
        "heuristic_imports": [],
    }


def scan_dependency_manifests(root_dir: str | Path) -> dict[str, Any]:
    root = Path(root_dir).resolve()
    if not root.is_dir():
        return {
            "python_packages": [],
            "entrypoints": [],
            "unsupported": [],
            "warnings": [f"Dependency manifest directory not found: {root_dir}"],
            "sources": [],
            "heuristic_imports": [],
        }
    packages: list[str] = []
    unsupported: list[str] = []
    warnings: list[str] = []
    sources: list[dict[str, str]] = []
    _scan_manifests(root, packages, unsupported, warnings, sources)
    return {
        "python_packages": _dedupe(packages),
        "entrypoints": [],
        "unsupported": _dedupe(unsupported),
        "warnings": _dedupe(warnings),
        "sources": sources,
        "heuristic_imports": [],
    }


def aggregate_dependency_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    packages: list[str] = []
    entrypoints: list[str] = []
    unsupported: list[str] = []
    warnings: list[str] = []
    sources: list[dict[str, str]] = []
    heuristic_imports: list[str] = []
    for report in reports:
        packages.extend(str(item) for item in report.get("python_packages") or [])
        entrypoints.extend(str(item) for item in report.get("entrypoints") or [])
        unsupported.extend(str(item) for item in report.get("unsupported") or [])
        warnings.extend(str(item) for item in report.get("warnings") or [])
        heuristic_imports.extend(str(item) for item in report.get("heuristic_imports") or [])
        for source in report.get("sources") or []:
            if isinstance(source, dict):
                sources.append(source)
    return {
        "python_packages": _dedupe(packages),
        "entrypoints": _dedupe(entrypoints),
        "unsupported": _dedupe(unsupported),
        "warnings": _dedupe(warnings),
        "sources": sources,
        "heuristic_imports": _dedupe(heuristic_imports),
    }


def _scan_frontmatter(
    root: Path,
    packages: list[str],
    entrypoints: list[str],
    unsupported: list[str],
    warnings: list[str],
    sources: list[dict[str, str]],
) -> None:
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        return
    try:
        frontmatter, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        warnings.append(f"Cannot read SKILL.md: {exc}")
        return
    metadata = frontmatter.get("metadata") if isinstance(frontmatter, dict) else None
    if not isinstance(metadata, dict):
        metadata = {}
    candidates = [
        metadata.get("python"),
        frontmatter.get("python"),
        frontmatter.get("dependencies"),
        frontmatter.get("prerequisites"),
        frontmatter.get("requirements"),
    ]
    hermes = metadata.get("hermes")
    if isinstance(hermes, dict):
        candidates.append(hermes.get("prerequisites"))
    openclaw = metadata.get("openclaw")
    if isinstance(openclaw, dict):
        candidates.extend([openclaw.get("requires"), openclaw.get("install")])
    for candidate in candidates:
        _extract_structured_dependency_value(candidate, packages, entrypoints, unsupported)
    if any(candidate for candidate in candidates):
        sources.append({"path": "SKILL.md", "kind": "frontmatter"})


def _scan_compiled_activation_contract(
    root: Path,
    packages: list[str],
    unsupported: list[str],
    warnings: list[str],
    sources: list[dict[str, str]],
) -> None:
    """Forward compiler-owned package prerequisites to the managed runtime.

    Nested worker/bootstrap/aggregation declarations are not visible to a
    flat frontmatter scanner. Reusing the bounded Skill compiler prevents a
    second YAML interpretation from confusing worker-DAG dependencies with
    Python packages.
    """
    try:
        from skills.loader import load_skill_content

        loaded = load_skill_content(
            root / "SKILL.md",
            skill_dir=str(root),
        )
    except Exception as exc:
        warnings.append(
            f"Could not compile activation dependency contract: {type(exc).__name__}: {exc}"
        )
        return
    execution = loaded.get("execution_contract") if isinstance(loaded, dict) else None
    if not isinstance(execution, dict):
        return
    diagnostics = execution.get("diagnostics")
    if isinstance(diagnostics, dict):
        for item in diagnostics.get("errors") or []:
            code = item.get("code") if isinstance(item, dict) else "invalid_contract"
            unsupported.append(f"invalid_skill_contract:{code}")

    environments: list[dict[str, Any]] = []
    package_environment = execution.get("environment_contract")
    if isinstance(package_environment, dict):
        environments.append(package_environment)
    for worker in execution.get("workers") or []:
        environment = (
            worker.get("environment_contract")
            if isinstance(worker, dict) else None
        )
        if isinstance(environment, dict):
            environments.append(environment)
    bootstrap = execution.get("knowledge_bootstrap")
    if isinstance(bootstrap, dict):
        for source in bootstrap.get("sources") or []:
            environment = (
                source.get("environment_contract")
                if isinstance(source, dict) else None
            )
            if isinstance(environment, dict):
                environments.append(environment)
    aggregation = execution.get("aggregation")
    if isinstance(aggregation, dict):
        for step in aggregation.get("steps") or []:
            environment = (
                step.get("environment_contract")
                if isinstance(step, dict) else None
            )
            if isinstance(environment, dict):
                environments.append(environment)

    compiled_packages = [
        str(item.get("requirement"))
        for environment in environments
        for item in environment.get("packages") or []
        if isinstance(item, dict)
        and item.get("optional") is not True
        and item.get("requirement")
    ]
    if compiled_packages:
        packages.extend(compiled_packages)
        sources.append({"path": "SKILL.md", "kind": "activation_contract"})


def _extract_structured_dependency_value(
    value: Any,
    packages: list[str],
    entrypoints: list[str],
    unsupported: list[str],
) -> None:
    if not value:
        return
    if isinstance(value, str):
        _extract_install_text(value, packages, unsupported)
        return
    if isinstance(value, list):
        for item in value:
            _extract_structured_dependency_value(item, packages, entrypoints, unsupported)
        return
    if not isinstance(value, dict):
        return
    for key in ("packages", "python_packages", "pip", "pip_packages", "dependencies", "deps"):
        _extract_structured_dependency_value(value.get(key), packages, entrypoints, unsupported)
    for key in ("entrypoint", "entrypoints", "scripts"):
        entry_value = value.get(key)
        if isinstance(entry_value, str):
            entrypoints.append(entry_value)
        elif isinstance(entry_value, list):
            entrypoints.extend(str(item) for item in entry_value if item)
    for key in ("bins", "apt", "system", "system_packages", "conda"):
        unsupported_value = value.get(key)
        if isinstance(unsupported_value, str):
            unsupported.append(unsupported_value)
        elif isinstance(unsupported_value, list):
            unsupported.extend(str(item) for item in unsupported_value if item)


def _extract_install_text(text: str, packages: list[str], unsupported: list[str]) -> None:
    for line in text.splitlines() or [text]:
        stripped = line.strip()
        if not stripped:
            continue
        if "pip install" in stripped:
            after = stripped.split("pip install", 1)[1]
            packages.extend(_requirements_from_text(after))
        elif stripped.startswith(("apt ", "apt-get ", "conda ", "npm ", "npx ")):
            unsupported.append(stripped)
        else:
            packages.extend(_requirements_from_text(stripped))


def _scan_manifests(
    root: Path,
    packages: list[str],
    unsupported: list[str],
    warnings: list[str],
    sources: list[dict[str, str]],
) -> None:
    """Read only package-root runtime manifests.

    Agent Skill resources are disclosed and selected on demand.  A manifest
    nested below ``assets/``, ``references/``, ``examples/``, ``tests/``, or an
    unselected script subtree therefore cannot be treated as a package-wide
    runtime declaration.  Doing so lets inert examples install packages or
    block every entrypoint in the Skill.  Component-specific dependencies must
    instead be declared by the selected execution contract (or be self-contained
    in the selected script); only conventional package-root manifests have
    package-wide authority here.
    """

    for path in _iter_package_runtime_manifests(root):
        name = path.name
        rel = _rel(path, root)
        try:
            if name == RUNTIME_REQUIREMENTS_NAME:
                parsed = _requirements_from_text(path.read_text(encoding="utf-8", errors="replace"))
                if parsed:
                    packages.extend(parsed)
                    sources.append({"path": rel, "kind": "requirements"})
            elif name == "pyproject.toml":
                parsed = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
                deps = _pyproject_dependencies(parsed)
                if deps:
                    packages.extend(deps)
                    sources.append({"path": rel, "kind": "pyproject"})
            elif name == "setup.cfg":
                deps = _setup_cfg_dependencies(path)
                if deps:
                    packages.extend(deps)
                    sources.append({"path": rel, "kind": "setup_cfg"})
            elif name == "Pipfile":
                deps = _pipfile_dependencies(path)
                if deps:
                    packages.extend(deps)
                    sources.append({"path": rel, "kind": "pipfile"})
            elif name in {"environment.yml", "environment.yaml"}:
                pip_deps, conda_deps = _environment_yml_dependencies(path)
                if pip_deps:
                    packages.extend(pip_deps)
                if conda_deps:
                    unsupported.extend(conda_deps)
                if pip_deps or conda_deps:
                    sources.append({"path": rel, "kind": "environment_yml"})
        except (OSError, tomllib.TOMLDecodeError, configparser.Error) as exc:
            warnings.append(f"Failed to parse {rel}: {exc}")


def _iter_package_runtime_manifests(root: Path):
    """Yield exact root-level manifests with unambiguous runtime semantics."""

    try:
        children = sorted(root.iterdir())
    except OSError:
        return
    for path in children:
        if not path.is_file() or path.is_symlink():
            continue
        if path.name == RUNTIME_REQUIREMENTS_NAME or path.name in MANIFEST_NAMES:
            yield path


def _requirements_from_text(text: str) -> list[str]:
    deps: list[str] = []
    for raw in text.splitlines() or [text]:
        line = raw.strip()
        if len(line) >= 2 and line[0] == line[-1] and line[0] in {"'", '"'}:
            line = line[1:-1].strip()
        if not line or REQ_OPTION_RE.match(line):
            continue
        try:
            Requirement(line)
        except InvalidRequirement:
            candidates: list[str] = []
            try:
                tokens = [token for token in shlex.split(line) if not token.startswith("-")]
            except ValueError:
                tokens = []
            if len(tokens) > 1:
                try:
                    for token in tokens:
                        Requirement(token)
                except InvalidRequirement:
                    candidates = []
                else:
                    candidates = tokens
            if not candidates and "," in line:
                comma_parts = [part.strip() for part in line.split(",") if part.strip()]
                try:
                    for part in comma_parts:
                        Requirement(part)
                except InvalidRequirement:
                    comma_parts = []
                candidates = comma_parts
            if candidates:
                deps.extend(candidates)
            elif (
                REQ_NAME_RE.match(line)
                or line.startswith(("http://", "https://", "git+", "file:"))
                or " @ " in line
            ):
                # Preserve invalid/direct declarations so the sidecar's PEP
                # 508 evaluator can reject them explicitly; silently dropping
                # an unparseable dependency would be an unsafe success.
                deps.append(line)
        else:
            deps.append(line)
    return deps


def _pyproject_dependencies(parsed: dict[str, Any]) -> list[str]:
    deps: list[str] = []
    project = parsed.get("project")
    if isinstance(project, dict):
        deps.extend(str(item) for item in project.get("dependencies") or [] if item)
        # Optional dependency groups are not package-wide runtime requirements.
        # A selected route may declare one explicitly in its execution contract.
    poetry = (((parsed.get("tool") or {}).get("poetry") or {}) if isinstance(parsed.get("tool"), dict) else {})
    poetry_deps = poetry.get("dependencies") if isinstance(poetry, dict) else None
    if isinstance(poetry_deps, dict):
        for name, spec in poetry_deps.items():
            if str(name).lower() == "python":
                continue
            deps.append(_format_poetry_dep(name, spec))
    return deps


def _format_poetry_dep(name: str, spec: Any) -> str:
    if isinstance(spec, str):
        return name if spec in {"*", ""} else f"{name}{spec if spec.startswith(('=', '<', '>', '~', '!')) else '==' + spec}"
    if isinstance(spec, dict):
        version = spec.get("version")
        extras = spec.get("extras")
        label = name
        if isinstance(extras, list) and extras:
            label += "[" + ",".join(str(item) for item in extras) + "]"
        if isinstance(version, str) and version and version != "*":
            label += version if version.startswith(("=", "<", ">", "~", "!")) else "==" + version
        return label
    return name


def _setup_cfg_dependencies(path: Path) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    deps: list[str] = []
    if parser.has_option("options", "install_requires"):
        deps.extend(_requirements_from_text(parser.get("options", "install_requires")))
    return deps


def _pipfile_dependencies(path: Path) -> list[str]:
    parsed = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    deps: list[str] = []
    for section in ("packages",):
        values = parsed.get(section)
        if not isinstance(values, dict):
            continue
        for name, spec in values.items():
            deps.append(_format_poetry_dep(str(name), spec))
    return deps


def _environment_yml_dependencies(path: Path) -> tuple[list[str], list[str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    pip_deps: list[str] = []
    conda_deps: list[str] = []
    in_pip = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "- pip:" or stripped == "pip:":
            in_pip = True
            continue
        if in_pip and stripped.startswith("-"):
            pip_deps.extend(_requirements_from_text(stripped[1:].strip()))
            continue
        in_pip = False
        if stripped.startswith("-"):
            item = stripped[1:].strip()
            if item and not item.startswith("python") and not item.startswith("pip"):
                conda_deps.append(item)
    return pip_deps, conda_deps


def _scan_mcp_entrypoints(
    root: Path,
    entrypoints: list[str],
    warnings: list[str],
    sources: list[dict[str, str]],
) -> None:
    mcp_json = root / ".mcp.json"
    if mcp_json.is_file():
        try:
            raw = json.loads(mcp_json.read_text(encoding="utf-8", errors="replace"))
            servers = raw.get("mcpServers", raw.get("servers", {}))
            if isinstance(servers, dict):
                for cfg in servers.values():
                    if isinstance(cfg, dict) and cfg.get("command") in {"python", "python3"}:
                        for arg in cfg.get("args") or []:
                            if isinstance(arg, str) and arg.endswith(".py"):
                                entrypoints.append(arg)
                sources.append({"path": ".mcp.json", "kind": "mcp_config"})
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"Failed to parse .mcp.json: {exc}")
    root_mcp = sorted(path for path in root.glob("*.py") if "mcp" in path.name.lower())
    for path in root_mcp:
        entrypoints.append(_rel(path, root))


def _scan_python_imports(root: Path, manifest_packages: list[str], warnings: list[str]) -> list[str]:
    known = {_package_name(dep).lower() for dep in manifest_packages}
    imports: set[str] = set()
    for path in _iter_files(root):
        if path.suffix not in PYTHON_EXTENSIONS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError) as exc:
            warnings.append(f"Could not inspect imports in {_rel(path, root)}: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imports.add(node.module.split(".", 1)[0])
    result: list[str] = []
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    for name in sorted(imports):
        if name in stdlib or name.startswith("_") or name in BUILTIN_PACKAGES:
            continue
        package = IMPORT_PACKAGE_HINTS.get(name, name)
        if package.lower() in known:
            continue
        result.append(package)
    return result


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def _package_name(requirement: str) -> str:
    match = REQ_NAME_RE.match(requirement)
    return match.group(1) if match else requirement


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
