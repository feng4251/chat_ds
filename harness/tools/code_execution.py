"""Python execution through a dedicated network-isolated container."""

from __future__ import annotations

import ast
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from tools.approval import check_code_danger, check_code_warnings
from tools.context import ToolContext
from tools.execution_fence import require_execution_authority
from tools.omission_guard import compacted_history_omission_error, contains_compacted_history_omission
from tools.path_security import SANDBOX_ROOT, sandbox_dir
from tools.session_sandbox_policy import (
    SessionSandboxPolicyError,
    session_sandbox_egress_budget_binding,
    session_sandbox_public_read_enabled,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_CODE_BYTES = 900_000
MAX_SNAPSHOT_FILES = 120
MAX_SNAPSHOT_FILE_BYTES = 200_000
MAX_SNAPSHOT_TOTAL_BYTES = 650_000
MAX_CODE_WITH_SNAPSHOT_BYTES = 500_000
SNAPSHOT_EXTENSIONS = {
    ".py", ".json", ".csv", ".tsv", ".txt", ".md", ".yaml", ".yml",
}
SKILL_DATA_ROOT = Path(os.environ.get("SKILL_DATA_ROOT", "/app/data/skills"))
_PIP_INSTALL_RE = re.compile(r"\bpip\s+install\b|subprocess\.(?:run|Popen|call)\([^\n]*(?:pip|python\s+-m\s+pip)", re.IGNORECASE)
_NETWORK_IMPORT_RE = re.compile(
    r"(^|\n)\s*(?:import\s+(requests|httpx|urllib|aiohttp|socket)\b|from\s+(requests|httpx|urllib|aiohttp|socket)\b)",
    re.IGNORECASE,
)
_NETWORK_CALL_RE = re.compile(r"(?:requests|httpx)\.(?:get|post|put|delete|request|stream)\s*\(|urllib\.request\.urlopen\s*\(|aiohttp\.ClientSession\s*\(|socket\.(?:create_connection|socket)\s*\(|subprocess\.(?:run|Popen|call)\([^\n]*(?:curl|wget)", re.IGNORECASE)
_EXTERNAL_PATH_RE = re.compile(r'''/(?:app/data/skills|app/workspace|nfs/temp/chat_ds|tmp/exec_[A-Za-z0-9_]+)[^'"\s)]*''')
_REMOTE_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

_PANDAS_READERS = {"read_csv", "read_json", "read_excel", "read_parquet"}
_PANDAS_WRITERS = {"to_csv", "to_json", "to_excel", "to_parquet"}
_NUMPY_READERS = {"load", "loadtxt"}
_NUMPY_WRITERS = {"save", "savetxt"}
_PATH_READ_METHODS = {
    "read_text", "read_bytes", "stat", "exists", "is_file", "is_dir",
    "iterdir", "glob", "rglob", "open",
}
_PATH_WRITE_METHODS = {
    "write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir",
    "rename", "replace", "chmod", "symlink_to", "hardlink_to",
}
_OS_READERS = {
    "os.listdir", "os.scandir", "os.stat", "os.walk", "os.chdir",
    "os.path.getsize", "os.path.exists", "os.path.isfile", "os.path.isdir",
    "glob.glob", "glob.iglob",
}
_OS_WRITERS = {
    "os.makedirs", "os.mkdir", "os.remove", "os.unlink", "os.rmdir",
    "os.rename", "os.replace",
}
_SHUTIL_WRITERS = {
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree",
    "shutil.move", "shutil.rmtree", "shutil.copymode", "shutil.copystat",
}


class _ExplicitFileOperationVisitor(ast.NodeVisitor):
    """Find actual file API calls, without matching comments or string contents."""

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}
        self.values: dict[str, str] = {}
        self.path_objects: set[str] = set()
        self.paths: list[str] = []
        self.has_read = False
        self.has_write = False
        self.has_remote = False

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self.aliases[item.asname or item.name.split(".", 1)[0]] = item.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            for item in node.names:
                self.aliases[item.asname or item.name] = f"{node.module}.{item.name}"

    def visit_Assign(self, node: ast.Assign) -> None:
        value = self._path_value(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if self._is_path_expression(node.value):
                    self.path_objects.add(target.id)
                else:
                    self.path_objects.discard(target.id)
                if value is None:
                    self.values.pop(target.id, None)
                else:
                    self.values[target.id] = value
        self.generic_visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            if self._is_path_expression(node.value):
                self.path_objects.add(node.target.id)
            else:
                self.path_objects.discard(node.target.id)
            value = self._path_value(node.value)
            if value is None:
                self.values.pop(node.target.id, None)
            else:
                self.values[node.target.id] = value
            self.visit(node.value)

    def _qualified_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._qualified_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    def _path_value(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr) and all(isinstance(item, ast.Constant) for item in node.values):
            return "".join(str(item.value) for item in node.values)
        if isinstance(node, ast.Name):
            return self.values.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            left, right = self._path_value(node.left), self._path_value(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            return str(PurePosixPath(left) / right)
        if isinstance(node, ast.Call):
            name = self._qualified_name(node.func) or ""
            if name in {"str", "os.fspath"} and node.args:
                return self._path_value(node.args[0])
            if name in {"pathlib.Path", "pathlib.PurePath", "pathlib.PurePosixPath", "Path", "PurePath", "PurePosixPath"}:
                return self._path_value(node.args[0]) if node.args else "."
            if name in {"pathlib.Path.cwd", "Path.cwd"}:
                return "."
            if name == "os.path.join" and node.args:
                parts = [self._path_value(arg) for arg in node.args]
                if all(part is not None for part in parts):
                    return str(PurePosixPath(parts[0], *parts[1:]))
            if isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
                base = self._path_value(node.func.value)
                parts = [self._path_value(arg) for arg in node.args]
                if base is not None and all(part is not None for part in parts):
                    return str(PurePosixPath(base, *parts))
        return None

    def _is_path_expression(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.path_objects
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._is_path_expression(node.left)
        if isinstance(node, ast.Attribute) and node.attr in {"parent", "parents"}:
            return self._is_path_expression(node.value)
        if isinstance(node, ast.Call):
            name = self._qualified_name(node.func) or ""
            if name in {
                "pathlib.Path", "pathlib.PurePath", "pathlib.PurePosixPath",
                "Path", "PurePath", "PurePosixPath", "pathlib.Path.cwd", "Path.cwd",
            }:
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "joinpath", "resolve", "absolute", "with_name", "with_suffix",
            }:
                return self._is_path_expression(node.func.value)
        return False

    @staticmethod
    def _argument(node: ast.Call, position: int, keywords: tuple[str, ...] = ()) -> ast.AST | None:
        if len(node.args) > position:
            return node.args[position]
        for keyword in node.keywords:
            if keyword.arg in keywords:
                return keyword.value
        return None

    def _record(self, expr: ast.AST | None, *, write: bool, remote_is_network: bool = False) -> None:
        # An omitted optional output path (for example DataFrame.to_json()) returns
        # in-memory data and is not a workspace operation.
        if expr is None:
            return
        value = self._path_value(expr)
        if value is not None:
            stripped = value.strip()
            # Literal JSON/CSV bodies are data, not filenames.
            if not stripped or "\x00" in stripped or "\n" in stripped or "\r" in stripped:
                return
            if stripped.startswith(("{", "[")):
                return
            if _REMOTE_URI_RE.match(stripped):
                if remote_is_network:
                    self.has_remote = True
                return
            self.paths.append(stripped)
        elif isinstance(expr, ast.Call) and (self._qualified_name(expr.func) or "").endswith(("StringIO", "BytesIO")):
            return
        # A dynamic argument to a real file API is still file I/O, not pure computation.
        if write:
            self.has_write = True
        else:
            self.has_read = True

    def visit_Call(self, node: ast.Call) -> None:
        name = self._qualified_name(node.func) or ""
        terminal = name.rsplit(".", 1)[-1]

        if (
            isinstance(node.func, ast.Name) and name in {"open", "builtins.open"}
        ) or name == "io.open":
            mode_node = self._argument(node, 1, ("mode",))
            mode = self._path_value(mode_node) if mode_node is not None else "r"
            self._record(self._argument(node, 0, ("file",)), write=bool(mode and any(flag in mode for flag in "wax+")))
        elif name.startswith("pandas.") and terminal in _PANDAS_READERS:
            self._record(self._argument(node, 0, ("filepath_or_buffer", "path_or_buf", "io", "excel_file", "path")), write=False, remote_is_network=True)
        elif terminal in _PANDAS_WRITERS:
            self._record(self._argument(node, 0, ("path_or_buf", "excel_writer", "path")), write=True, remote_is_network=True)
        elif name.startswith("numpy.") and terminal in _NUMPY_READERS:
            self._record(self._argument(node, 0, ("file", "fname")), write=False)
        elif name.startswith("numpy.") and terminal in _NUMPY_WRITERS:
            self._record(self._argument(node, 0, ("file", "fname")), write=True)
        elif name in _OS_READERS:
            self._record(self._argument(node, 0, ("path", "top", "pathname", "root_dir")), write=False)
        elif name in _OS_WRITERS:
            self._record(self._argument(node, 0, ("path", "name", "src")), write=True)
            if terminal in {"rename", "replace"}:
                self._record(self._argument(node, 1, ("dst",)), write=True)
        elif name in _SHUTIL_WRITERS:
            self._record(self._argument(node, 0, ("src", "path")), write=True)
            if terminal != "rmtree":
                self._record(self._argument(node, 1, ("dst",)), write=True)
        elif (
            isinstance(node.func, ast.Attribute)
            and terminal in _PATH_READ_METHODS | _PATH_WRITE_METHODS
            and (
                self._path_value(node.func.value) is not None
                or self._is_path_expression(node.func.value)
            )
        ):
            path_method_writes = terminal in _PATH_WRITE_METHODS
            if terminal == "open":
                mode_node = self._argument(node, 0, ("mode",))
                mode = self._path_value(mode_node) if mode_node is not None else "r"
                path_method_writes = bool(mode and any(flag in mode for flag in "wax+"))
            self._record(node.func.value, write=path_method_writes)
            if terminal in {"glob", "rglob"}:
                # The pattern is interpreted relative to the Path base and can itself
                # contain traversal components, so include it in boundary validation.
                self._record(self._argument(node, 0, ("pattern",)), write=False)
            if terminal in {"rename", "replace", "symlink_to", "hardlink_to"}:
                self._record(self._argument(node, 0, ("target",)), write=True)
        elif name in {"subprocess.run", "subprocess.Popen", "subprocess.call"} and node.args:
            command = node.args[0]
            values: list[str] = []
            if isinstance(command, (ast.List, ast.Tuple)):
                values = [value for item in command.elts if (value := self._path_value(item)) is not None]
            elif (value := self._path_value(command)) is not None:
                values = value.split()
            if values and PurePosixPath(values[0]).name in {"cat", "wc", "stat", "ls", "find", "grep", "head", "tail", "cp", "mv"}:
                self.has_read = True
                for value in values[1:]:
                    if not value.startswith("-"):
                        self._record(ast.Constant(value), write=PurePosixPath(values[0]).name in {"cp", "mv"})

        self.generic_visit(node)


def _inspect_file_operations(code: str) -> tuple[bool, bool, bool, tuple[str, ...]]:
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, TypeError):
        return False, False, False, ()
    visitor = _ExplicitFileOperationVisitor()
    visitor.visit(tree)
    return visitor.has_read, visitor.has_write, visitor.has_remote, tuple(visitor.paths)


def _managed_runtime_reason(code: str) -> str | None:
    if _NETWORK_IMPORT_RE.search(code) or _NETWORK_CALL_RE.search(code):
        return "network/API code"
    has_read, has_write, has_remote, _ = _inspect_file_operations(code)
    if has_remote:
        return "network/API code"
    if has_write:
        return "workspace file-write code"
    if has_read:
        return "workspace file operation code"
    return None


def _requires_managed_runtime(code: str) -> bool:
    return _managed_runtime_reason(code) is not None


def _execution_boundary_error(code: str, user_id: str, session_id: str) -> str | None:
    if _PIP_INSTALL_RE.search(code):
        return (
            "Inline pip install is not allowed in execute_code. The managed session runtime "
            "installs declared skill dependencies automatically; remove pip install lines and retry, "
            "or run a declared skill script with run_skill_python."
        )
    for match in _EXTERNAL_PATH_RE.finditer(code):
        path = match.group(0)
        if path.startswith("/tmp/exec_"):
            return (
                "execute_code runs in a fresh ephemeral executor. Use stable relative paths "
                "under skills/... and workspace/...; do not reuse /tmp/exec_* paths from previous calls."
            )
        if path.startswith("/app/workspace") or _is_current_session_absolute_path(path, user_id, session_id):
            continue
        return (
            "Absolute paths are limited to the current session workspace/skills. Use stable relative paths "
            "under skills/... and workspace/...; do not access another session or host path."
        )
    _, _, _, explicit_paths = _inspect_file_operations(code)
    for path in explicit_paths:
        posix = PurePosixPath(path)
        windows = PureWindowsPath(path)
        if ".." in posix.parts or ".." in windows.parts:
            return (
                "Relative workspace paths must not traverse outside the session workspace; "
                "remove '..' path components and retry."
            )
        if posix.is_absolute():
            if path == "/app/workspace" or path.startswith("/app/workspace/") or _is_current_session_absolute_path(path, user_id, session_id):
                continue
            return (
                "Absolute paths are limited to the current session workspace/skills. Use stable relative paths "
                "under skills/... and workspace/...; do not access another session or host path."
            )
        if windows.is_absolute():
            return (
                "Absolute paths are limited to the current session workspace/skills. Use stable relative paths "
                "under skills/... and workspace/...; do not access host paths."
            )
    return None


def _is_current_session_absolute_path(path: str, user_id: str, session_id: str) -> bool:
    if not user_id or user_id == "default" or not session_id or session_id == "default":
        return False
    allowed_prefixes = (
        f"{SKILL_DATA_ROOT}/{user_id}/{session_id}/",
        f"{SANDBOX_ROOT}/{user_id}/{session_id}/workspace/",
    )
    return path.startswith(allowed_prefixes)


def _safe_snapshot_relpath(path: Path, root: Path, prefix: str) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    if path.suffix.lower() not in SNAPSHOT_EXTENSIONS:
        return None
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if any(part in {"", ".", ".."} for part in rel.parts):
        return None
    return str(PurePosixPath(prefix, *rel.parts))


def _snapshot_file(path: Path, root: Path, prefix: str, files: list[dict], budget: dict) -> bool:
    rel = _safe_snapshot_relpath(path, root, prefix)
    if rel is None or any(existing.get("path") == rel for existing in files):
        return False
    if budget["files"] >= MAX_SNAPSHOT_FILES or budget["bytes"] >= MAX_SNAPSHOT_TOTAL_BYTES:
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size > MAX_SNAPSHOT_FILE_BYTES or budget["bytes"] + size > MAX_SNAPSHOT_TOTAL_BYTES:
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except UnicodeDecodeError:
        return False
    content = content.encode("utf-8", errors="replace").decode("utf-8")
    files.append({"path": rel, "content": content})
    budget["files"] += 1
    budget["bytes"] += size
    return True


def _snapshot_files_from_root(root: Path, prefix: str, files: list[dict], budget: dict) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if budget["files"] >= MAX_SNAPSHOT_FILES or budget["bytes"] >= MAX_SNAPSHOT_TOTAL_BYTES:
            return
        _snapshot_file(path, root, prefix, files, budget)


def _referenced_session_files(code: str, user_id: str, session_id: str) -> list[tuple[Path, Path, str]]:
    refs: list[tuple[Path, Path, str]] = []
    roots = [
        ((SKILL_DATA_ROOT / user_id / session_id).resolve(), f"{SKILL_DATA_ROOT}/{user_id}/{session_id}/", "skills"),
        ((SANDBOX_ROOT / user_id / session_id / "workspace").resolve(), f"{SANDBOX_ROOT}/{user_id}/{session_id}/workspace/", "workspace"),
    ]
    for root, marker, prefix in roots:
        if marker not in code:
            continue
        for match in re.finditer(re.escape(marker) + r"[^'\"\s)]+", code):
            rel_text = match.group(0)[len(marker):]
            rel = PurePosixPath(rel_text)
            if rel.is_absolute() or ".." in rel.parts:
                continue
            path = (root / Path(*rel.parts)).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            refs.append((path, root, prefix))
    return refs


def _referenced_persisted_result_paths(
    code: str,
    user_id: str,
    session_id: str,
) -> tuple[str, ...]:
    """Resolve only literal, current-session ``results/`` file references."""

    if not user_id or user_id == "default" or not session_id or session_id == "default":
        return ()
    _, _, _, explicit_paths = _inspect_file_operations(code)
    root = SANDBOX_ROOT / user_id / session_id / "results"
    if not root.is_dir() or root.is_symlink():
        return ()
    try:
        verified_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return ()

    selected: set[str] = set()
    for raw_path in explicit_paths:
        path = PurePosixPath(raw_path)
        if path.is_absolute() or len(path.parts) < 2 or path.parts[0] != "results":
            continue
        relative = PurePosixPath(*path.parts[1:])
        if any(part in {"", ".", ".."} for part in relative.parts):
            continue
        candidate = root.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            lexical = candidate.lstat()
            resolved.relative_to(verified_root)
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate.is_symlink() or not candidate.is_file() or lexical.st_nlink != 1:
            continue
        if Path(os.path.abspath(candidate)) != resolved:
            continue
        selected.add(relative.as_posix())
    return tuple(sorted(selected))


def _session_snapshot(user_id: str, session_id: str, code: str = "") -> list[dict]:
    if not user_id or user_id == "default" or not session_id or session_id == "default":
        return []
    files: list[dict] = []
    budget = {"files": 0, "bytes": 0}
    for path, root, prefix in _referenced_session_files(code, user_id, session_id):
        _snapshot_file(path, root, prefix, files, budget)
    return files


def _code_with_session_snapshot(code: str, user_id: str, session_id: str) -> str:
    files = _session_snapshot(user_id, session_id, code)
    if not files:
        return code
    rewritten = _rewrite_session_absolute_paths(code, user_id, session_id)
    total_file_bytes = sum(len(str(item.get("content", "")).encode("utf-8", errors="replace")) for item in files)
    estimated_payload_bytes = len(rewritten.encode("utf-8", errors="replace")) + total_file_bytes
    if estimated_payload_bytes > MAX_CODE_WITH_SNAPSHOT_BYTES:
        return (
            "# ChatDS skipped automatic session snapshot injection because the workspace/skill snapshot is too large.\n"
            "# Read needed files explicitly from stable relative paths under workspace/ or skills/.\n"
            + rewritten
        )
    prelude = (
        "import json as __chatds_json, pathlib as __chatds_pathlib\n"
        f"__chatds_files = __chatds_json.loads({json.dumps(json.dumps(files, ensure_ascii=False))})\n"
        "for __chatds_file in __chatds_files:\n"
        "    __chatds_path = __chatds_pathlib.Path(__chatds_file['path'])\n"
        "    __chatds_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    __chatds_path.write_text(__chatds_file['content'], encoding='utf-8', errors='replace')\n"
        "    if __chatds_file['path'].startswith('workspace/'):\n"
        "        __chatds_workspace_path = __chatds_pathlib.Path(__chatds_file['path'][10:])\n"
        "        __chatds_workspace_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "        __chatds_workspace_path.write_text(__chatds_file['content'], encoding='utf-8', errors='replace')\n"
        "del __chatds_json, __chatds_pathlib, __chatds_files, __chatds_file, __chatds_path\n"
        "try:\n"
        "    del __chatds_workspace_path\n"
        "except NameError:\n"
        "    pass\n"
    )
    return prelude + rewritten


def _rewrite_session_absolute_paths(code: str, user_id: str, session_id: str) -> str:
    rewritten = code.replace("/app/workspace/", "workspace/").replace("/app/workspace", "workspace")
    if not user_id or user_id == "default" or not session_id or session_id == "default":
        return rewritten
    skill_abs_prefix = f"{SKILL_DATA_ROOT}/{user_id}/{session_id}/"
    workspace_abs_prefix = f"{SANDBOX_ROOT}/{user_id}/{session_id}/workspace/"
    return rewritten.replace(skill_abs_prefix, "skills/").replace(workspace_abs_prefix, "workspace/")


def _rewrite_isolated_session_paths(code: str, user_id: str, session_id: str) -> str:
    """Map public session paths to the sidecar's disposable directory layout."""

    rewritten = code.replace("/app/workspace/", "./").replace("/app/workspace", ".")
    rewritten = re.sub(r'''([(['"])(?:\./)?workspace/''', r"\1./", rewritten)
    if user_id and user_id != "default" and session_id and session_id != "default":
        workspace_abs_prefix = f"{SANDBOX_ROOT}/{user_id}/{session_id}/workspace/"
        skill_abs_prefix = f"{SKILL_DATA_ROOT}/{user_id}/{session_id}/"
        rewritten = rewritten.replace(workspace_abs_prefix, "./")
        rewritten = rewritten.replace(skill_abs_prefix, "../skills/")
    rewritten = re.sub(r'''([(['"])(?:\./)?skills/''', r"\1../skills/", rewritten)
    return re.sub(r'''([(['"])(?:\./)?results/''', r"\1../results/", rewritten)


def _references_session_skills(code: str, user_id: str, session_id: str) -> bool:
    if re.search(r'''(?:^|[(['"\s])(?:\./)?skills/''', code):
        return True
    if user_id and user_id != "default" and session_id and session_id != "default":
        return f"{SKILL_DATA_ROOT}/{user_id}/{session_id}/" in code
    return False


async def execute_code(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    user_id: str = "default",
    session_id: str = "default",
    context: ToolContext | None = None,
) -> str:
    """Run Python only in a dedicated container with no network namespace."""
    if not code or not code.strip():
        return json.dumps({"status": "error", "error": "No code provided."})
    if contains_compacted_history_omission(code):
        return json.dumps(compacted_history_omission_error("code"), ensure_ascii=False)
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return json.dumps({"status": "error", "error": "Code payload is too large."})

    danger = check_code_danger(code)
    if danger:
        return json.dumps({"status": "blocked", "error": danger})
    boundary_error = _execution_boundary_error(code, user_id, session_id)
    if boundary_error:
        return json.dumps({"status": "blocked", "error": boundary_error}, ensure_ascii=False)

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    managed_reason = _managed_runtime_reason(code)
    if managed_reason:
        from tools.isolated_skill_executor import (
            IsolatedSkillExecutorError,
            execute_isolated_session_code,
        )

        workspace = sandbox_dir(user_id, session_id, sub="workspace")
        session_skills = SKILL_DATA_ROOT / user_id / session_id
        skills_root = (
            session_skills
            if _references_session_skills(code, user_id, session_id) and session_skills.is_dir()
            else None
        )
        isolated_code = _rewrite_isolated_session_paths(code, user_id, session_id)
        result_paths = _referenced_persisted_result_paths(code, user_id, session_id)
        results_root = (
            SANDBOX_ROOT / user_id / session_id / "results"
            if result_paths
            else None
        )
        try:
            public_read = (
                context is not None
                and session_sandbox_public_read_enabled()
            )
            egress_binding = (
                session_sandbox_egress_budget_binding(
                    context,
                    operation="execute_code",
                )
                if public_read
                else None
            )
        except SessionSandboxPolicyError as exc:
            return json.dumps({
                "status": "error",
                "error_code": "invalid_session_sandbox_policy",
                "error": str(exc),
            }, ensure_ascii=False)
        try:
            result = await execute_isolated_session_code(
                workspace=workspace,
                code=isolated_code,
                timeout=timeout,
                skills_root=skills_root,
                results_root=results_root,
                result_paths=result_paths,
                **(
                    {
                        "public_read": {
                            "methods": ["GET", "HEAD"],
                            "ports": [80, 443],
                        },
                        "budget_scope_sha256": (
                            egress_binding.budget_scope_sha256
                        ),
                        "call_id_sha256": egress_binding.call_id_sha256,
                    }
                    if public_read and egress_binding is not None
                    else {}
                ),
                **(
                    {
                        "execution_authority_check": lambda: (
                            require_execution_authority(
                                context,
                                boundary="execute_code.executor_commit",
                            )
                        )
                    }
                    if (
                        context is not None
                        and context.execution_fence is not None
                    )
                    else {}
                ),
            )
        except IsolatedSkillExecutorError as exc:
            logger.warning("Isolated session-code request failed safely: %s", exc.code)
            result = {
                "status": "error",
                "error_code": exc.code,
                "error": str(exc),
                "network": (
                    "controlled_egress" if public_read else "disabled"
                ),
                "artifacts": [],
                "workspace_applied": False,
            }
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        result.setdefault("output", stdout)
        if result.get("status") == "error" and stderr:
            result["output"] = stdout + ("\n--- stderr ---\n" if stdout else "") + stderr
        if "returncode" in result:
            result.setdefault("exit_code", result["returncode"])
        result["execution_runtime"] = "isolated_session_python"
        result["execution_note"] = (
            f"execute_code detected {managed_reason} and ran it only in the disposable, "
            "session executor. Workspace changes were content-verified before "
            "atomic application; execution never falls back to the harness container."
        )
        if managed_reason == "network/API code":
            result["network_access"] = (
                "controlled_public_read" if public_read else "unavailable"
            )
            if not public_read:
                result["degraded_reason"] = (
                    "Network access is disabled for model-authored code. Use "
                    "an explicitly authorized web/API tool or declared Skill "
                    "capability; execute_code will not retry in harness."
                )
        warnings = check_code_warnings(code)
        if warnings:
            result["warnings"] = warnings
        return json.dumps(result, ensure_ascii=False)

    code = _code_with_session_snapshot(code, user_id, session_id)
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return json.dumps({"status": "error", "error": "Code plus session snapshot is too large."})
    from tools.isolated_skill_executor import (
        IsolatedSkillExecutorError,
        execute_isolated_legacy_code,
    )

    try:
        result = await execute_isolated_legacy_code(
            code=code,
            timeout=timeout,
            **(
                {
                    "execution_authority_check": lambda: (
                        require_execution_authority(
                            context,
                            boundary="execute_code.executor_commit",
                        )
                    )
                }
                if (
                    context is not None
                    and context.execution_fence is not None
                )
                else {}
            ),
        )
        warnings = check_code_warnings(code)
        if warnings:
            result["warnings"] = warnings
        return json.dumps(result, ensure_ascii=False)
    except IsolatedSkillExecutorError as exc:
        logger.warning(
            "Isolated compatibility-code request failed safely: %s",
            exc.code,
        )
        return json.dumps({
            "status": "error",
            "error_code": exc.code,
            "error": str(exc),
            "network": "disabled",
        }, ensure_ascii=False)
    except Exception as exc:
        logger.exception("Isolated executor request failed")
        return json.dumps({
            "status": "error",
            "error": (
                "The isolated code executor is unavailable: "
                f"{type(exc).__name__}: {exc}"
            ),
        }, ensure_ascii=False)


EXECUTE_CODE_SCHEMA = {
    "name": "execute_code",
    "description": (
        "Run Python code for calculations and data processing. By default this uses a dedicated, "
        "ephemeral container with networking disabled and only explicitly referenced session files "
        "snapshotted under ./workspace, ./skills, or the current session's read-only ./results namespace. "
        "If the code imports/calls network libraries such as "
        "requests/httpx/urllib/aiohttp/socket, writes files, or reads/stats/globs workspace files, "
        "execute_code runs that single call in a disposable session-aware sidecar, still with networking "
        "disabled; verified file changes are atomically applied to the session workspace. It never falls "
        "back to Python inside the harness container. Network requests fail explicitly; use an authorized "
        "web/API capability instead. Inline pip install is never allowed. Do not use execute_code to carry long Markdown/report bodies; "
        "write large artifacts with write_file/patch_file or run a real workspace/skill script with run_skill_python. "
        "Use stable relative paths under skills/..., workspace/..., and results/...; "
        "do not access other sessions or reuse /tmp/exec_* paths from prior calls. "
        f"Default timeout is {DEFAULT_TIMEOUT}s; maximum is {MAX_TIMEOUT}s."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source code to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum execution time in seconds (default {DEFAULT_TIMEOUT}).",
                "default": DEFAULT_TIMEOUT,
                "minimum": 1,
                "maximum": MAX_TIMEOUT,
            },
        },
        "required": ["code"],
    },
}
