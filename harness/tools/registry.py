"""Tool registry — ToolEntry-based registration, schema retrieval, and dispatch.

Enhanced from the hermes-agent pattern. Thread-safe for async FastAPI usage.
"""

from __future__ import annotations

import difflib
import inspect
import json
import logging
from typing import Any, Callable, Dict, List, Optional, get_args, get_origin, get_type_hints

from tools.context import ToolContext
from tools.omission_guard import (
    compacted_history_omission_error,
    contains_compacted_history_omission,
)

logger = logging.getLogger(__name__)


def _is_tool_context_annotation(annotation: Any) -> bool:
    if annotation is inspect.Signature.empty:
        return False
    if annotation is ToolContext:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    return any(arg is ToolContext for arg in get_args(annotation))


def _find_omission_path(value: Any, path: str = "args") -> str | None:
    if contains_compacted_history_omission(value):
        return path
    if isinstance(value, dict):
        for key, item in value.items():
            found = _find_omission_path(item, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_omission_path(item, f"{path}[{index}]")
            if found:
                return found
    return None


# ── ToolEntry ──────────────────────────────────────────────────────────────

class ToolEntry:
    """Metadata for a single registered tool."""

    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn",
        "is_async", "description", "emoji",
        "accepts_context", "accepts_user_id", "accepts_session_id",
        "accepts_enabled_user_skills", "is_read_only", "is_destructive",
        "parallel_safe", "path_scoped", "allow_in_child",
        "allow_in_parallel_child", "mutates_workspace", "mutates_global_state",
        "requires_user_visibility",
    )

    def __init__(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable | None = None,
        is_async: bool = True,
        description: str = "",
        emoji: str = "",
        is_read_only: bool = False,
        is_destructive: bool = False,
        parallel_safe: bool = False,
        path_scoped: bool = False,
        allow_in_child: bool = True,
        allow_in_parallel_child: bool | None = None,
        mutates_workspace: bool | None = None,
        mutates_global_state: bool = False,
        requires_user_visibility: bool = False,
    ):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.is_async = is_async
        self.description = description or schema.get("description", "")
        self.emoji = emoji
        try:
            signature = inspect.signature(handler)
            params = signature.parameters
            type_hints = get_type_hints(handler)
        except (TypeError, ValueError):
            params = {}
            type_hints = {}
        self.accepts_context = any(
            name == "context" and _is_tool_context_annotation(type_hints.get(name, param.annotation))
            for name, param in params.items()
        )
        self.accepts_user_id = "user_id" in params
        self.accepts_session_id = "session_id" in params
        self.accepts_enabled_user_skills = "enabled_user_skills" in params
        self.is_read_only = is_read_only
        self.is_destructive = is_destructive
        self.parallel_safe = parallel_safe
        self.path_scoped = path_scoped
        self.allow_in_child = allow_in_child
        self.allow_in_parallel_child = parallel_safe if allow_in_parallel_child is None else allow_in_parallel_child
        self.mutates_workspace = (not is_read_only and path_scoped) if mutates_workspace is None else mutates_workspace
        self.mutates_global_state = mutates_global_state
        self.requires_user_visibility = requires_user_visibility


# ── ToolRegistry ───────────────────────────────────────────────────────────

class ToolRegistry:
    """Singleton registry that collects tool schemas + handlers from tool files."""

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}

    # ── Registration ──────────────────────────────────────────────────

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable | None = None,
        is_async: bool = True,
        description: str = "",
        emoji: str = "",
        is_read_only: bool = False,
        is_destructive: bool = False,
        parallel_safe: bool = False,
        path_scoped: bool = False,
        allow_in_child: bool = True,
        allow_in_parallel_child: bool | None = None,
        mutates_workspace: bool | None = None,
        mutates_global_state: bool = False,
        requires_user_visibility: bool = False,
    ):
        """Register a tool. Called at module-import time by each tool file."""
        existing = self._tools.get(name)
        if existing and existing.toolset != toolset:
            logger.warning(
                "Tool '%s' re-registered: toolset '%s' replacing '%s'",
                name, toolset, existing.toolset,
            )
        self._tools[name] = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            is_async=is_async,
            description=description,
            emoji=emoji,
            is_read_only=is_read_only,
            is_destructive=is_destructive,
            parallel_safe=parallel_safe,
            path_scoped=path_scoped,
            allow_in_child=allow_in_child,
            allow_in_parallel_child=allow_in_parallel_child,
            mutates_workspace=mutates_workspace,
            mutates_global_state=mutates_global_state,
            requires_user_visibility=requires_user_visibility,
        )

    def deregister(self, name: str) -> None:
        """Remove a tool from the registry."""
        self._tools.pop(name, None)

    # ── Schema retrieval ──────────────────────────────────────────────

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        """Return a registered tool entry by name, or None."""
        return self._tools.get(name)

    def get_definitions(self, tool_names: list[str]) -> list[dict]:
        """Return OpenAI-format tool schemas for the requested tool names.

        Only tools whose ``check_fn()`` returns True (or have no check_fn)
        are included.
        """
        result = []
        for name in tool_names:
            entry = self._tools.get(name)
            if not entry:
                continue
            if entry.check_fn:
                try:
                    if not entry.check_fn():
                        continue
                except Exception:
                    continue
            schema_with_name = {**entry.schema, "name": entry.name}
            result.append({"type": "function", "function": schema_with_name})
        return result

    def get_all_names(self) -> list[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def get_schema(self, name: str) -> Optional[dict]:
        """Return a tool's raw schema dict (no check_fn filtering)."""
        entry = self._tools.get(name)
        return entry.schema if entry else None

    def get_metadata(self, name: str) -> Optional[dict]:
        """Return harness-only execution metadata for a tool."""
        entry = self._tools.get(name)
        if not entry:
            return None
        return {
            "read_only": entry.is_read_only,
            "destructive": entry.is_destructive,
            "parallel_safe": entry.parallel_safe,
            "path_scoped": entry.path_scoped,
            "allow_in_child": entry.allow_in_child,
            "allow_in_parallel_child": entry.allow_in_parallel_child,
            "mutates_workspace": entry.mutates_workspace,
            "mutates_global_state": entry.mutates_global_state,
            "requires_user_visibility": entry.requires_user_visibility,
            "toolset": entry.toolset,
        }

    def get_emoji(self, name: str, default: str = "⚡") -> str:
        """Return the emoji for a tool, or *default* if unset."""
        entry = self._tools.get(name)
        return (entry.emoji if entry and entry.emoji else default)

    def _validate_args(self, entry: ToolEntry, args: Any) -> str | None:
        if not isinstance(args, dict):
            return f"Tool {entry.name} arguments must be a JSON object; got {type(args).__name__}."
        if "_raw_args" in args:
            return (
                f"Tool {entry.name} received reserved field _raw_args. "
                f"Retry with valid JSON matching this schema: {self._schema_hint(entry)}"
            )
        if "__tool_arg_parse_error" in args:
            return (
                f"Tool {entry.name} arguments were malformed JSON: "
                f"{str(args.get('__tool_arg_parse_error'))[:300]}. "
                f"Retry with valid JSON matching this schema: {self._schema_hint(entry)}"
            )

        params = entry.schema.get("parameters") or {}
        properties = params.get("properties") or {}
        required = params.get("required") or []
        for field in required:
            if field not in args:
                return (
                    f"Tool {entry.name} missing required field '{field}'. "
                    f"Retry with this schema: {self._schema_hint(entry)}"
                )

        allow_extra = params.get("additionalProperties") is True or not properties
        if not allow_extra:
            unexpected = sorted(k for k in args if k not in properties)
            if unexpected:
                return (
                    f"Tool {entry.name} received unexpected field(s): {', '.join(unexpected)}. "
                    f"Use only fields from this schema: {self._schema_hint(entry)}"
                )

        for field, value in args.items():
            spec = properties.get(field)
            if not isinstance(spec, dict):
                continue
            allowed = spec.get("enum")
            if allowed is not None and value not in allowed:
                return (
                    f"Tool {entry.name} field '{field}' must be one of {allowed}; got {value!r}."
                )
            expected = spec.get("type")
            if expected and not self._matches_json_type(value, expected):
                return (
                    f"Tool {entry.name} field '{field}' must be {expected}; "
                    f"got {type(value).__name__}."
                )
        return None

    def _schema_hint(self, entry: ToolEntry) -> dict:
        params = entry.schema.get("parameters") or {}
        properties = params.get("properties") or {}
        return {
            "required": list(params.get("required") or []),
            "properties": {
                key: value.get("type", "any") if isinstance(value, dict) else "any"
                for key, value in properties.items()
            },
        }

    @staticmethod
    def _matches_json_type(value: Any, expected: Any) -> bool:
        if isinstance(expected, list):
            return any(ToolRegistry._matches_json_type(value, item) for item in expected)
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        if expected == "null":
            return value is None
        return True

    @staticmethod
    def _strip_context_owned_args(entry: ToolEntry, args: Any) -> Any:
        if not isinstance(args, dict):
            return args
        stripped = dict(args)
        if entry.accepts_user_id:
            stripped.pop("user_id", None)
        if entry.accepts_session_id:
            stripped.pop("session_id", None)
        if entry.accepts_enabled_user_skills:
            stripped.pop("enabled_user_skills", None)
        if entry.accepts_context:
            stripped.pop("context", None)
        return stripped

    @staticmethod
    def _normalize_alias_args(entry: ToolEntry, args: Any) -> Any:
        if not isinstance(args, dict):
            return args
        if entry.name == "search_files" and "context" in args and "context_lines" not in args:
            normalized = dict(args)
            normalized["context_lines"] = normalized.pop("context")
            return normalized
        if entry.name in {"read_file", "write_file", "patch_file"} and "filepath" not in args:
            for alias in ("file_path", "path", "filename"):
                if alias in args:
                    normalized = dict(args)
                    normalized["filepath"] = normalized.pop(alias)
                    return normalized
        return args

    @staticmethod
    def _strip_unexpected_args(entry: ToolEntry, args: Any) -> tuple[Any, list[str]]:
        if not isinstance(args, dict):
            return args, []
        params = entry.schema.get("parameters") or {}
        properties = params.get("properties") or {}
        allow_extra = params.get("additionalProperties") is True or not properties
        if allow_extra:
            return args, []
        unexpected = sorted(k for k in args if k not in properties)
        if not unexpected:
            return args, []
        return {k: v for k, v in args.items() if k in properties}, unexpected

    @staticmethod
    def _append_ignored_args_notice(result: str, ignored_args: list[str]) -> str:
        notice = {
            "ignored_unexpected_fields": ignored_args,
            "hint": "Unexpected fields were ignored; use the tool schema fields only on future calls.",
        }
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return result + "\n\n" + json.dumps(notice, ensure_ascii=False)
        if isinstance(data, dict):
            data.update(notice)
            return json.dumps(data, ensure_ascii=False)
        return result + "\n\n" + json.dumps(notice, ensure_ascii=False)

    # ── Dispatch ──────────────────────────────────────────────────────

    def _unknown_tool_suggestions(self, name: str) -> list[str]:
        registered = sorted(self._tools)
        suggestions = difflib.get_close_matches(name, registered, n=5, cutoff=0.45)
        aliases = {
            "target_skill": ["skill_view", "run_skill_python", "skills_list"],
            "read_skill": ["skill_view", "skills_list"],
            "search_web": ["web_search"],
            "python": ["execute_code", "run_skill_python"],
        }
        for candidate in aliases.get(name, []):
            if candidate in self._tools and candidate not in suggestions:
                suggestions.append(candidate)
        return suggestions[:8]

    async def dispatch(
        self,
        name: str,
        args: dict,
        context: ToolContext | None = None,
    ) -> str:
        """Execute a tool handler by name with runtime-owned context."""
        entry = self._tools.get(name)
        if not entry:
            return tool_error(
                f"Unknown tool: {name}",
                unknown_tool=name,
                suggestions=self._unknown_tool_suggestions(name),
            )
        try:
            if context is not None:
                args = self._strip_context_owned_args(entry, args)
            args = self._normalize_alias_args(entry, args)
            omission_path = _find_omission_path(args)
            if omission_path:
                logger.info("Tool %s rejected compacted-history placeholder at %s", name, omission_path)
                return tool_error(
                    compacted_history_omission_error(omission_path)["error"],
                    reason="invalid_placeholder_content",
                    field=omission_path,
                )
            args, ignored_args = self._strip_unexpected_args(entry, args)
            schema_error = self._validate_args(entry, args)
            if schema_error:
                logger.info("Tool %s schema validation failed: %s", name, schema_error)
                return tool_error(schema_error)

            call_args = dict(args)
            if context is not None:
                if entry.accepts_user_id:
                    call_args["user_id"] = context.user_id
                if entry.accepts_session_id:
                    call_args["session_id"] = context.session_id
                if entry.accepts_enabled_user_skills:
                    call_args["enabled_user_skills"] = list(context.enabled_user_skills)
                if entry.accepts_context:
                    call_args["context"] = context
            if entry.is_async:
                result = await entry.handler(**call_args)
            else:
                result = entry.handler(**call_args)
            if ignored_args:
                result = self._append_ignored_args_notice(str(result), ignored_args)
            return str(result)
        except Exception as e:
            logger.exception("Tool %s dispatch error: %s", name, e)
            return tool_error(
                f"Tool execution failed: {type(e).__name__}: {e}. "
                "Check this tool's schema and retry with all required arguments; "
                "do not repeat empty or malformed args."
            )


# ── Module-level singleton ─────────────────────────────────────────────────

registry = ToolRegistry()


# ── Backward-compatible module-level functions ─────────────────────────────
# These delegate to the singleton so existing callers (agent.py) work unchanged.

def register(
    name: str,
    schema: dict,
    handler: Callable,
    toolset: str = "general",
    check_fn: Callable | None = None,
    is_async: bool = True,
    description: str = "",
    emoji: str = "",
    is_read_only: bool = False,
    is_destructive: bool = False,
    parallel_safe: bool = False,
    path_scoped: bool = False,
    allow_in_child: bool = True,
    allow_in_parallel_child: bool | None = None,
    mutates_workspace: bool | None = None,
    mutates_global_state: bool = False,
    requires_user_visibility: bool = False,
):
    """Register a tool with the default registry."""
    registry.register(
        name=name,
        toolset=toolset,
        schema=schema,
        handler=handler,
        check_fn=check_fn,
        is_async=is_async,
        description=description,
        emoji=emoji,
        is_read_only=is_read_only,
        is_destructive=is_destructive,
        parallel_safe=parallel_safe,
        path_scoped=path_scoped,
        allow_in_child=allow_in_child,
        allow_in_parallel_child=allow_in_parallel_child,
        mutates_workspace=mutates_workspace,
        mutates_global_state=mutates_global_state,
        requires_user_visibility=requires_user_visibility,
    )


def get_schemas(names: list[str]) -> list[dict]:
    """Return OpenAI-formatted tool definitions for the requested tool names."""
    return registry.get_definitions(names)


def get_metadata(name: str) -> Optional[dict]:
    """Return harness-only execution metadata for a registered tool."""
    return registry.get_metadata(name)


async def dispatch(
    name: str,
    args: dict,
    context: ToolContext | None = None,
) -> str:
    """Execute a registered tool with explicit runtime context."""
    return await registry.dispatch(name, args, context=context)


# ── Helpers for tool response serialization ────────────────────────────────

def tool_error(message: str, **extra) -> str:
    """Return a JSON error string for tool handlers."""
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    """Return a JSON result string for tool handlers.

    Accepts a dict positional arg *or* keyword arguments (not both).
    """
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)
