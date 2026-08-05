"""Transactional, content-addressed Claude plugin views for one Session."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


MAX_SKILL_VIEW_FILES = 8_192
MAX_SKILL_VIEW_BYTES = 512 * 1024 * 1024
MAX_SKILL_VIEW_FILE_BYTES = 64 * 1024 * 1024
MAX_SKILL_MCP_SERVERS = 128
MAX_SKILL_MCP_CONFIG_BYTES = 2 * 1024 * 1024


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
) -> ClaudeSkillView:
    """Publish one immutable plugin tree or reuse its verified digest path."""

    source_rows = tuple(sources)
    runtime_root = Path(session_root) / "runtime" / "claude" / "skill-views"
    _ensure_real_directory_chain(runtime_root)
    staging = runtime_root / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    plugin = staging / "plugin"
    skills_root = plugin / "skills"
    (plugin / ".claude-plugin").mkdir(parents=True, mode=0o700)
    skills_root.mkdir(parents=True, mode=0o700)

    manifest_skills: list[dict[str, Any]] = []
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

        mcp_servers = _compile_explicit_mcp_servers(
            sources=source_rows,
            plugin_root=plugin,
        )
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
            "name": "chatds-session-skills",
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
        except FileExistsError:
            _make_tree_owner_writable(staging)
            shutil.rmtree(staging)
            _verify_existing_view(target, view_sha256)
        _fsync_directory(runtime_root)
        return ClaudeSkillView(
            root=target,
            plugin_root=target / "plugin",
            sha256=view_sha256,
            skill_names=tuple(row["name"] for row in manifest_skills),
            file_count=len(file_rows),
            size_bytes=total_bytes,
            mcp_server_names=tuple(mcp_servers),
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


def _compile_explicit_mcp_servers(
    *,
    sources: tuple[SkillViewSource, ...],
    plugin_root: Path,
) -> dict[str, dict[str, Any]]:
    """Compile only explicit Skill MCP declarations for ``--bare`` mode.

    Claude Code intentionally skips plugin MCP auto-discovery under
    ``--bare``.  The Harness therefore produces one immutable explicit config
    instead of silently losing a Skill capability or enabling ambient user/
    project MCP state.  Unsupported helper/OAuth/WebSocket forms fail closed.
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
