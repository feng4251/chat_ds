"""Transactional, content-addressed Claude plugin views for one Session."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .skill_contracts import (
    SkillContractError,
    compile_skill_contract,
    compile_skill_workflows,
    compile_skill_workers,
)
from native_tools import (
    PLATFORM_IO_CAPABILITY_SET,
    SCHEDULE_PLATFORM_CAPABILITY_ALIASES,
)
from native_mcp import (
    NativeMCPError,
    normalize_mcp_declaration,
    normalize_mcp_name,
)


MAX_SKILL_VIEW_FILES = 8_192
MAX_SKILL_VIEW_BYTES = 512 * 1024 * 1024
MAX_SKILL_VIEW_FILE_BYTES = 64 * 1024 * 1024
MAX_SKILL_MCP_SERVERS = 128
MAX_SKILL_MCP_CONFIG_BYTES = 2 * 1024 * 1024
CLAUDE_SKILL_PLUGIN_NAME = "chatds-session-skills"


class SkillViewError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SkillViewSource:
    name: str
    scope: str
    root: Path
    bundle_id: str | None = None
    bundle_role: str | None = None


@dataclass(frozen=True, slots=True)
class ClaudeSkillView:
    root: Path
    plugin_root: Path
    sha256: str
    skill_names: tuple[str, ...]
    file_count: int
    size_bytes: int
    mcp_server_names: tuple[str, ...] = ()
    entrypoint_skill_name: str | None = None
    selected_primary_skill_names: tuple[str, ...] = ()


def authorized_skill_sources(
    *,
    user_id: str,
    session_id: str,
    registry_rows: Iterable[Any],
    skills_data_dir: Path,
) -> tuple[SkillViewSource, ...]:
    """Resolve rows already authorized by the Backend, with Session priority."""

    selected: dict[str, SkillViewSource] = {}
    priority: dict[str, int] = {}
    for row in registry_rows:
        name = _safe_component(str(getattr(row, "name", "")), field="skill name")
        row_session = getattr(row, "session_id", None)
        if row_session not in {None, session_id}:
            raise SkillViewError("Skill registry row escapes the selected Session")
        scope = "session" if row_session == session_id else "user"
        score = 1 if scope == "session" else 0
        root = (
            Path(skills_data_dir) / user_id / session_id / name
            if scope == "session"
            else Path(skills_data_dir) / user_id / name
        )
        previous = priority.get(name, -1)
        if score < previous:
            continue
        if score == previous:
            raise SkillViewError(f"Duplicate authorized Skill identity: {name}")
        selected[name] = SkillViewSource(
            name=name,
            scope=scope,
            root=root,
            bundle_id=getattr(row, "bundle_id", None),
            bundle_role=getattr(row, "bundle_role", None),
        )
        priority[name] = score
    return tuple(selected[name] for name in sorted(selected, key=lambda value: (value.casefold(), value)))


def materialize_claude_skill_view(
    *,
    session_root: Path,
    sources: Iterable[SkillViewSource],
    platform_capabilities: Iterable[str] = (),
    session_mcp_servers: dict[str, dict[str, Any]] | None = None,
    web_search_url: str = "",
    market_data_url: str = "",
) -> ClaudeSkillView:
    """Publish one immutable plugin tree or reuse its verified digest path."""

    source_rows = tuple(sources)
    selected_primary_skill_names = tuple(
        source.name
        for source in source_rows
        if source.bundle_role != "supporting"
    )
    runtime_root = Path(session_root) / "runtime" / "claude" / "skill-views"
    _ensure_real_directory_chain(runtime_root)
    staging = runtime_root / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    plugin = staging / "plugin"
    skills_root = plugin / "skills"
    (plugin / ".claude-plugin").mkdir(parents=True, mode=0o700)
    skills_root.mkdir(parents=True, mode=0o700)

    manifest_skills: list[dict[str, Any]] = []
    artifact_contracts: list[dict[str, Any]] = []
    runtime_requirements: list[dict[str, Any]] = []
    skill_diagnostics: list[dict[str, str]] = []
    worker_agents: list[dict[str, str]] = []
    workflow_routes: list[dict[str, Any]] = []
    file_rows: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        for source in source_rows:
            root = _validated_skill_root(source.root)
            target = skills_root / source.name
            target.mkdir(mode=0o700)
            relative_files = _safe_regular_files(root)
            if not any(path.as_posix() == "SKILL.md" for path in relative_files):
                raise SkillViewError(f"Skill '{source.name}' has no regular SKILL.md")
            try:
                artifact_contract, requirements, diagnostics = compile_skill_contract(
                    skill_name=source.name,
                    root=root,
                    relative_files=relative_files,
                    primary=source.bundle_role != "supporting",
                )
                workers = compile_skill_workers(
                    skill_name=source.name,
                    root=root,
                    relative_files=relative_files,
                )
                workflows = (
                    compile_skill_workflows(
                        skill_name=source.name,
                        root=root,
                        relative_files=relative_files,
                        workers=workers,
                    )
                    if source.bundle_role != "supporting"
                    else []
                )
            except SkillContractError as exc:
                raise SkillViewError(str(exc)) from exc
            if artifact_contract is not None:
                artifact_contracts.append(artifact_contract)
            if any(requirements.values()):
                runtime_requirements.append({
                    "skill_name": source.name,
                    **requirements,
                })
            skill_diagnostics.extend(diagnostics)
            skill_files: list[dict[str, Any]] = []
            for relative in relative_files:
                if len(file_rows) >= MAX_SKILL_VIEW_FILES:
                    raise SkillViewError("Skill view file-count limit exceeded")
                destination = target.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest, size = _copy_regular_file(root / relative, destination)
                total_bytes += size
                if total_bytes > MAX_SKILL_VIEW_BYTES:
                    raise SkillViewError("Skill view byte limit exceeded")
                row = {
                    "path": f"plugin/skills/{source.name}/{relative.as_posix()}",
                    "sha256": digest,
                    "size": size,
                }
                file_rows.append(row)
                skill_files.append({"path": relative.as_posix(), "sha256": digest, "size": size})
            manifest_skills.append({
                "name": source.name,
                "scope": source.scope,
                "bundle_id": source.bundle_id,
                "bundle_role": source.bundle_role,
                "files": skill_files,
            })
            if workers:
                agents_root = plugin / "agents" / source.name
                agents_root.mkdir(parents=True, mode=0o700)
            for worker in workers:
                if len(file_rows) >= MAX_SKILL_VIEW_FILES:
                    raise SkillViewError("Skill view file-count limit exceeded")
                relative_agent_path = (
                    PurePosixPath("plugin")
                    / "agents"
                    / source.name
                    / f"{worker['worker_id']}.md"
                )
                payload = _render_native_worker_agent(worker).encode("utf-8")
                if len(payload) > MAX_SKILL_VIEW_FILE_BYTES:
                    raise SkillViewError(
                        "Generated native worker exceeds the view file limit"
                    )
                destination = staging.joinpath(*relative_agent_path.parts)
                _write_bytes_exclusive(destination, payload)
                digest = hashlib.sha256(payload).hexdigest()
                size = len(payload)
                total_bytes += size
                if total_bytes > MAX_SKILL_VIEW_BYTES:
                    raise SkillViewError("Skill view byte limit exceeded")
                os.chmod(destination, 0o444, follow_symlinks=False)
                file_rows.append({
                    "path": relative_agent_path.as_posix(),
                    "sha256": digest,
                    "size": size,
                })
                worker_agents.append({
                    "skill_name": source.name,
                    "worker_id": worker["worker_id"],
                    "source_path": worker["source_path"],
                    "agent_path": relative_agent_path.as_posix(),
                    "native_agent_type": (
                        f"{CLAUDE_SKILL_PLUGIN_NAME}:{source.name}:"
                        f"{worker['worker_id']}"
                    ),
                })
            for workflow in workflows:
                workflow_routes.append({
                    **{
                        key: value
                        for key, value in workflow.items()
                        if key != "phases"
                    },
                    "phases": [
                        {
                            "mode": phase["mode"],
                            "workers": [
                                {
                                    "worker_id": worker_id,
                                    "native_agent_type": (
                                        f"{CLAUDE_SKILL_PLUGIN_NAME}:"
                                        f"{source.name}:{worker_id}"
                                    ),
                                }
                                for worker_id in phase["worker_ids"]
                            ],
                        }
                        for phase in workflow["phases"]
                    ],
                })

        # Installed Skills remain available to Claude's native description-
        # based Skill router. Do not prepend a synthetic slash command: doing
        # so turns "installed" into "mandatory on every Turn" and anchors
        # unrelated follow-up requests to stale domain instructions.
        entrypoint_skill_name: str | None = None

        mcp_servers = _compile_explicit_mcp_servers(
            sources=source_rows,
            plugin_root=plugin,
        )
        for raw_name, raw_config in sorted(
            (session_mcp_servers or {}).items()
        ):
            try:
                name = normalize_mcp_name(raw_name)
                normalized_config = normalize_mcp_declaration(raw_config)
            except NativeMCPError as exc:
                raise SkillViewError(str(exc)) from exc
            if name in mcp_servers:
                raise SkillViewError(
                    f"Duplicate explicit MCP server identity: {name}"
                )
            mcp_servers[name] = normalized_config
        if any(
            row.get("persistent_stdin_process") is True
            for row in runtime_requirements
        ):
            server_name = "chatds-process"
            if server_name in mcp_servers:
                raise SkillViewError(
                    "Explicit Skill MCP identity conflicts with platform capability"
                )
            mcp_servers[server_name] = {
                "type": "stdio",
                "command": "/usr/local/bin/python",
                "args": ["-I", "-m", "claude_runner.mcp_process"],
            }
        platform_egress_rules: list[dict[str, Any]] = []
        projected_platform_capabilities: list[str] = []
        schedule_capability_aliases: dict[str, str] = {}
        enabled_platform_capabilities = {
            str(name)
            for name in platform_capabilities
            if isinstance(name, str)
        }
        if enabled_platform_capabilities - PLATFORM_IO_CAPABILITY_SET:
            raise SkillViewError("Unknown platform I/O capability")
        if "web_search" in enabled_platform_capabilities:
            normalized_search_url = _normalize_platform_web_search_url(
                web_search_url
            )
            server_name = "chatds-web-search"
            if server_name in mcp_servers:
                raise SkillViewError(
                    "Explicit Skill MCP identity conflicts with platform capability"
                )
            mcp_servers[server_name] = {
                "type": "stdio",
                "command": "/usr/local/bin/python",
                "args": ["-I", "-m", "claude_runner.mcp_web_search"],
                "env": {"CHATDS_SEARXNG_SEARCH_URL": normalized_search_url},
            }
            platform_egress_rules.append({
                "capability": "web_search",
                "url_prefix": normalized_search_url,
                "methods": ["GET"],
            })
        if "market_quote" in enabled_platform_capabilities:
            normalized_market_url = _normalize_platform_market_data_url(
                market_data_url
            )
            server_name = "chatds-market-data"
            if server_name in mcp_servers:
                raise SkillViewError(
                    "Explicit Skill MCP identity conflicts with platform capability"
                )
            mcp_servers[server_name] = {
                "type": "stdio",
                "command": "/usr/local/bin/python",
                "args": ["-I", "-m", "claude_runner.mcp_market_data"],
                "env": {"CHATDS_MARKET_DATA_URL": normalized_market_url},
            }
            platform_egress_rules.append({
                "capability": "market_quote",
                "url_prefix": normalized_market_url,
                "methods": ["GET"],
            })
        if "cronjob" in enabled_platform_capabilities:
            # The job store uses canonical ChatDS capability names, while the
            # model sees native and MCP-qualified names. Compile their exact
            # translation into this immutable view so every later boundary
            # consumes the same capability vocabulary.
            schedule_capability_aliases.update({
                name: name
                for name in sorted(enabled_platform_capabilities)
            })
            schedule_capability_aliases.update({
                alias: canonical
                for alias, canonical
                in SCHEDULE_PLATFORM_CAPABILITY_ALIASES.items()
                if canonical in enabled_platform_capabilities
            })
            server_name = "chatds-schedule"
            if server_name in mcp_servers:
                raise SkillViewError(
                    "Explicit Skill MCP identity conflicts with platform capability"
                )
            mcp_servers[server_name] = {
                "type": "stdio",
                "command": "/usr/local/bin/python",
                "args": ["-I", "-m", "claude_runner.mcp_schedule_control"],
                "env": {
                    "CHATDS_SCHEDULE_CAPABILITY_ALIASES_JSON": json.dumps(
                        dict(sorted(schedule_capability_aliases.items())),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            }
            projected_platform_capabilities.append("schedule_control")
        if len(mcp_servers) > MAX_SKILL_MCP_SERVERS:
            raise SkillViewError("Compiled MCP server-count limit exceeded")
        mcp_config_path = plugin / ".mcp.json"
        _write_json_exclusive(mcp_config_path, {"mcpServers": mcp_servers})
        mcp_config_bytes = mcp_config_path.read_bytes()
        if len(mcp_config_bytes) > MAX_SKILL_MCP_CONFIG_BYTES:
            raise SkillViewError("Compiled MCP configuration exceeds its limit")
        file_rows.append({
            "path": "plugin/.mcp.json",
            "sha256": hashlib.sha256(mcp_config_bytes).hexdigest(),
            "size": len(mcp_config_bytes),
        })
        total_bytes += len(mcp_config_bytes)

        plugin_descriptor = {
            "name": CLAUDE_SKILL_PLUGIN_NAME,
            "version": "1.0.0",
            "description": "Immutable ChatDS Session Skill view",
        }
        descriptor_path = plugin / ".claude-plugin" / "plugin.json"
        _write_json_exclusive(descriptor_path, plugin_descriptor)
        descriptor_bytes = descriptor_path.read_bytes()
        file_rows.append({
            "path": "plugin/.claude-plugin/plugin.json",
            "sha256": hashlib.sha256(descriptor_bytes).hexdigest(),
            "size": len(descriptor_bytes),
        })
        total_bytes += len(descriptor_bytes)
        identity = {
            "schema": "chatds.claude-skill-view.v1",
            "plugin_name": CLAUDE_SKILL_PLUGIN_NAME,
            "entrypoint_skill_name": entrypoint_skill_name,
            "selected_primary_skill_names": list(
                selected_primary_skill_names
            ),
            "platform_egress_rules": platform_egress_rules,
            "platform_capabilities": sorted(projected_platform_capabilities),
            "schedule_capability_aliases": dict(
                sorted(schedule_capability_aliases.items())
            ),
            "artifact_contracts": artifact_contracts,
            "runtime_requirements": runtime_requirements,
            "skill_diagnostics": skill_diagnostics,
            "worker_agents": worker_agents,
            "workflow_routes": workflow_routes,
            "skills": manifest_skills,
            "files": sorted(file_rows, key=lambda row: row["path"]),
        }
        view_sha256 = _canonical_sha256(identity)
        manifest = {**identity, "sha256": view_sha256}
        _write_json_exclusive(staging / "manifest.json", manifest)
        _make_tree_read_only(staging)

        target = runtime_root / view_sha256
        try:
            staging.rename(target)
        except OSError as exc:
            # POSIX local filesystems commonly report EEXIST when another
            # publisher already installed the same content-addressed tree.
            # NFSv3 reports ENOTEMPTY for the identical rename collision.
            # Treat only those two destination-exists forms as a CAS race;
            # the winner is still fully re-verified before reuse.
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            _make_tree_owner_writable(staging)
            shutil.rmtree(staging)
            _verify_existing_view(target, view_sha256)
        _fsync_directory(runtime_root)
        return ClaudeSkillView(
            root=target,
            plugin_root=target / "plugin",
            sha256=view_sha256,
            skill_names=tuple(source.name for source in source_rows),
            file_count=len(file_rows),
            size_bytes=total_bytes,
            mcp_server_names=tuple(mcp_servers),
            entrypoint_skill_name=entrypoint_skill_name,
            selected_primary_skill_names=selected_primary_skill_names,
        )
    except BaseException:
        if staging.exists():
            _make_tree_owner_writable(staging)
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _safe_component(value: str, *, field: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or len(value) > 128
    ):
        raise SkillViewError(f"Invalid {field}")
    return value


def _render_native_worker_agent(worker: dict[str, str]) -> str:
    skill_name = worker["skill_name"]
    worker_id = worker["worker_id"]
    authoritative_path = (
        "${CLAUDE_PLUGIN_ROOT}/skills/"
        f"{skill_name}/{worker['source_path']}"
    )
    return (
        "---\n"
        f"name: {json.dumps(worker_id, ensure_ascii=False)}\n"
        "description: "
        f"{json.dumps(worker['description'], ensure_ascii=False)}\n"
        "skills: "
        f"{json.dumps([skill_name], ensure_ascii=False)}\n"
        "model: inherit\n"
        "---\n\n"
        "This is a native subagent for a structured worker declared by the "
        "installed Skill.\n\n"
        "Read the authoritative worker definition below completely before "
        "acting:\n\n"
        f"`{authoritative_path}`\n\n"
        "Follow that definition exactly. Return its requested output to the "
        "parent agent through the native Agent lifecycle.\n"
    )


def _normalize_platform_web_search_url(value: object) -> str:
    """Validate the deployment-owned metasearch coordinate."""

    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(value, str) or not value or len(value) > 8192:
        raise SkillViewError("Harness web-search URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SkillViewError("Harness web-search URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or port is not None and not 1 <= port <= 65535
        or not parsed.path.rstrip("/").endswith("/search")
    ):
        raise SkillViewError("Harness web-search URL is invalid")
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path.rstrip("/"),
        "",
        "",
    ))


def _normalize_platform_market_data_url(value: object) -> str:
    """Validate the fixed typed quote-gateway coordinate."""

    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(value, str) or not value or len(value) > 8192:
        raise SkillViewError("Harness market-data URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SkillViewError("Harness market-data URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or port is not None and not 1 <= port <= 65535
        or parsed.path.rstrip("/") != "/v1/quote"
    ):
        raise SkillViewError("Harness market-data URL is invalid")
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        "/v1/quote",
        "",
        "",
    ))


def _compile_explicit_mcp_servers(
    *,
    sources: tuple[SkillViewSource, ...],
    plugin_root: Path,
) -> dict[str, dict[str, Any]]:
    """Compile only explicit Skill MCP declarations for the isolated Runner.

    The Harness produces one immutable explicit config instead of silently
    losing a Skill capability or enabling ambient user/project MCP state.
    Unsupported helper/OAuth/WebSocket forms fail closed.
    """

    compiled: dict[str, dict[str, Any]] = {}
    for source in sources:
        source_path = source.root / ".mcp.json"
        if not source_path.is_file():
            continue
        target_path = plugin_root / "skills" / source.name / ".mcp.json"
        try:
            raw = json.loads(target_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise SkillViewError(
                f"Skill '{source.name}' has an invalid .mcp.json"
            ) from exc
        if not isinstance(raw, dict):
            raise SkillViewError(
                f"Skill '{source.name}' has an invalid .mcp.json"
            )
        servers = raw.get("mcpServers", raw.get("servers"))
        if not isinstance(servers, dict):
            raise SkillViewError(
                f"Skill '{source.name}' .mcp.json has no server map"
            )
        for raw_name, raw_config in servers.items():
            name = _safe_mcp_server_name(raw_name)
            if name in compiled:
                raise SkillViewError(
                    f"Duplicate explicit MCP server identity: {name}"
                )
            compiled[name] = _normalize_mcp_server_config(
                raw_config,
                source=source,
            )
            if len(compiled) > MAX_SKILL_MCP_SERVERS:
                raise SkillViewError("Explicit MCP server-count limit exceeded")
    return compiled


def _safe_mcp_server_name(value: object) -> str:
    name = str(value or "")
    if (
        not name
        or len(name) > 64
        or name in {".", ".."}
        or any(
            not (
                character.isascii()
                and (character.isalnum() or character in "._-")
            )
            for character in name
        )
    ):
        raise SkillViewError("Invalid explicit MCP server identity")
    return name


def _normalize_mcp_server_config(
    value: object,
    *,
    source: SkillViewSource,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillViewError("Explicit MCP server config must be an object")
    server_type = str(value.get("type") or ("stdio" if value.get("command") else ""))
    runtime_root = f"/skill-view/plugin/skills/{source.name}"
    source_root = str(Path(source.root).absolute())

    def substitute(raw: object, *, limit: int = 8192) -> str:
        if (
            not isinstance(raw, str)
            or not raw
            or len(raw) > limit
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
        ):
            raise SkillViewError("Explicit MCP config contains an invalid string")
        result = raw
        for marker in (
            source_root,
            "${CLAUDE_PLUGIN_ROOT}",
            "${SKILL_DIR}",
            "{{skill_dir}}",
        ):
            result = result.replace(marker, runtime_root)
        return result

    if server_type == "stdio":
        if set(value) - {"type", "command", "args", "env"}:
            raise SkillViewError("Unsupported stdio MCP configuration field")
        command = substitute(value.get("command"), limit=4096)
        raw_args = value.get("args", [])
        if not isinstance(raw_args, list) or len(raw_args) > 128:
            raise SkillViewError("Explicit MCP args are invalid")
        args = [substitute(item) for item in raw_args]
        raw_env = value.get("env")
        env = None
        if raw_env is not None:
            if not isinstance(raw_env, dict) or len(raw_env) > 128:
                raise SkillViewError("Explicit MCP environment is invalid")
            env = {
                substitute(key, limit=256): substitute(item, limit=32_768)
                for key, item in raw_env.items()
            }
        return {
            "type": "stdio",
            "command": command,
            "args": args,
            **({"env": env} if env is not None else {}),
        }

    if server_type in {"http", "sse"}:
        if set(value) - {"type", "url", "headers"}:
            raise SkillViewError("Unsupported remote MCP configuration field")
        url = substitute(value.get("url"), limit=8192)
        try:
            from urllib.parse import urlsplit

            parsed = urlsplit(url)
            parsed_port = parsed.port
        except ValueError as exc:
            raise SkillViewError("Explicit MCP URL is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed_port is not None and not 1 <= parsed_port <= 65535
        ):
            raise SkillViewError("Explicit MCP URL is invalid")
        raw_headers = value.get("headers")
        headers = None
        if raw_headers is not None:
            if not isinstance(raw_headers, dict) or len(raw_headers) > 128:
                raise SkillViewError("Explicit MCP headers are invalid")
            headers = {
                substitute(key, limit=256): substitute(item, limit=32_768)
                for key, item in raw_headers.items()
            }
        return {
            "type": server_type,
            "url": url,
            **({"headers": headers} if headers is not None else {}),
        }
    raise SkillViewError("Unsupported explicit MCP transport")


def _validated_skill_root(root: Path) -> Path:
    lexical = Path(os.path.abspath(os.fspath(root)))
    _reject_symlink_components(lexical)
    try:
        mode = os.lstat(lexical).st_mode
    except OSError as exc:
        raise SkillViewError("Authorized Skill directory is unavailable") from exc
    if not stat.S_ISDIR(mode):
        raise SkillViewError("Authorized Skill root is not a directory")
    return lexical


def _safe_regular_files(root: Path) -> tuple[PurePosixPath, ...]:
    values: list[PurePosixPath] = []
    for walk_root, dirs, files in os.walk(root, followlinks=False):
        current = Path(walk_root)
        safe_dirs: list[str] = []
        for name in sorted(dirs):
            path = current / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise SkillViewError("Skill directory contains a non-directory or symlink")
            safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in sorted(files):
            path = current / name
            mode = os.lstat(path).st_mode
            if not stat.S_ISREG(mode):
                raise SkillViewError("Skill package contains a non-regular file")
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if any(part in {"", ".", ".."} for part in relative.parts):
                raise SkillViewError("Skill package contains an unsafe path")
            values.append(relative)
            if len(values) > MAX_SKILL_VIEW_FILES:
                raise SkillViewError("Skill view file-count limit exceeded")
    return tuple(sorted(values, key=lambda value: (value.as_posix().casefold(), value.as_posix())))


def _copy_regular_file(source: Path, destination: Path) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(source, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        source_mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(source_mode):
            raise SkillViewError("Skill resource changed type during publication")
        with os.fdopen(fd, "rb", closefd=False) as source_stream:
            with destination.open("xb") as destination_stream:
                while True:
                    block = source_stream.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_SKILL_VIEW_FILE_BYTES:
                        raise SkillViewError("Skill resource exceeds the view file limit")
                    digest.update(block)
                    destination_stream.write(block)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
        os.chmod(
            destination,
            0o555 if source_mode & 0o111 else 0o444,
            follow_symlinks=False,
        )
    finally:
        os.close(fd)
    return digest.hexdigest(), size


def _write_json_exclusive(path: Path, value: object) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _write_bytes_exclusive(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_existing_view(root: Path, expected_sha256: str) -> None:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SkillViewError("Existing Skill view cannot be verified") from exc
    claimed = manifest.pop("sha256", None) if isinstance(manifest, dict) else None
    if claimed != expected_sha256 or _canonical_sha256(manifest) != expected_sha256:
        raise SkillViewError("Existing Skill view identity mismatch")
    for row in manifest.get("files", []):
        if not isinstance(row, dict):
            raise SkillViewError("Existing Skill view manifest is malformed")
        relative = PurePosixPath(str(row.get("path") or ""))
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise SkillViewError("Existing Skill view path is unsafe")
        path = root.joinpath(*relative.parts)
        mode = os.lstat(path).st_mode
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise SkillViewError("Existing Skill view resource is not regular")
        payload = path.read_bytes()
        if len(payload) != row.get("size") or hashlib.sha256(payload).hexdigest() != row.get("sha256"):
            raise SkillViewError("Existing Skill view resource digest mismatch")


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise SkillViewError("Path contains a symbolic link")
        except FileNotFoundError:
            return


def _ensure_real_directory_chain(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(path)
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise SkillViewError("Skill view root is not a directory")


def _make_tree_read_only(root: Path) -> None:
    for walk_root, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(walk_root) / name
            mode = os.lstat(path).st_mode
            os.chmod(
                path,
                0o555 if mode & 0o111 else 0o444,
                follow_symlinks=False,
            )
        for name in dirs:
            os.chmod(Path(walk_root) / name, 0o555, follow_symlinks=False)
    os.chmod(root, 0o555, follow_symlinks=False)


def _make_tree_owner_writable(root: Path) -> None:
    for walk_root, dirs, files in os.walk(root, followlinks=False):
        os.chmod(walk_root, 0o700, follow_symlinks=False)
        for name in files:
            try:
                os.chmod(Path(walk_root) / name, 0o600, follow_symlinks=False)
            except OSError:
                pass
        for name in dirs:
            try:
                os.chmod(Path(walk_root) / name, 0o700, follow_symlinks=False)
            except OSError:
                pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
