"""Fail-closed assembly for streamed OpenAI-compatible tool calls.

Providers do not all follow OpenAI's fragment conventions precisely.  In
particular, some reuse (or omit) ``index`` while starting a new call.  This
module treats a provider call id as identity and uses an index only to route an
id-less continuation.  It also validates the complete batch before callers
are allowed to dispatch any tool.

The debug summary is deliberately structural: it contains counts and error
codes, never provider ids, argument text, or other request payload values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


_MAX_LOGICAL_TOOL_CALLS = 128
# Some OpenAI-compatible providers emit one structured delta per token (and,
# for JSON strings, occasionally close to one delta per character).  A fixed
# 8K transport-fragment ceiling therefore truncated otherwise bounded 20--30K
# tool arguments before JSON validation.  Keep the transport abuse ceiling in
# line with ``StreamConvergenceGuard``; the substantially tighter logical-call,
# argument-character, JSON-depth/node, request-deadline, and provider-fragment
# limits remain the authoritative bounds.
_MAX_STREAM_FRAGMENTS = 1_000_000
_MAX_ARGUMENT_CHARS_PER_CALL = 8 * 1024 * 1024
_MAX_ARGUMENT_CHARS_PER_BATCH = 16 * 1024 * 1024
_MAX_ARGUMENT_JSON_DEPTH = 64
_MAX_ARGUMENT_JSON_NODES = 20_000
_MAX_ARGUMENT_RECONSTRUCTION_CANDIDATES = 32
_MAX_ARGUMENT_RECONSTRUCTION_STATE_CHARS = 2 * _MAX_ARGUMENT_CHARS_PER_CALL
_MAX_ARGUMENT_RECONSTRUCTION_STATE_CHARS_PER_BATCH = (
    2 * _MAX_ARGUMENT_CHARS_PER_BATCH
)
_MAX_ARGUMENT_DIAGNOSTIC_PROFILES = 8
_MIN_SHORTER_SNAPSHOT_PREFIX_CHARS = 4


def json_argument_structure_error(value: Any) -> str | None:
    """Return a stable error code for attacker-shaped JSON containers."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_ARGUMENT_JSON_DEPTH:
            return "arguments_nesting_limit_exceeded"
        nodes += 1
        if nodes > _MAX_ARGUMENT_JSON_NODES:
            return "arguments_node_limit_exceeded"
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return None


@dataclass(frozen=True)
class AssembledStreamToolCall:
    """One validated logical call ready for transport-neutral dispatch."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolCallStreamAssembly:
    """Atomic batch plus locally complete calls for bounded recovery review."""

    calls: tuple[AssembledStreamToolCall, ...]
    errors: tuple[str, ...]
    debug: dict[str, Any]
    complete_calls: tuple[AssembledStreamToolCall, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.calls)


@dataclass
class _LogicalCall:
    ordinal: int
    provider_id: str | None = None
    provider_indexes: set[str] = field(default_factory=set)
    name_candidates: set[str] = field(default_factory=lambda: {""})
    locked_name: str | None = None
    # The standards path is authoritative and append-only.  Keeping chunks
    # avoids quadratic string rebuilding for providers that emit one argument
    # character per delta.
    standard_argument_parts: list[str] = field(default_factory=list)
    standard_argument_chars: int = 0
    standard_in_string: bool = False
    standard_trailing_escape: bool = False
    # Compatibility paths are activated only by evidence of cumulative/reset
    # snapshots.  They can recover non-standard providers, but can never
    # invalidate a complete standards-path JSON object.
    argument_candidates: set[str] = field(default_factory=set)
    # A provider that starts a new, non-prefix-related root object after an
    # already complete argument object has exposed two possible final values.
    # Keep the earlier complete value immutable so compatibility recovery
    # cannot silently turn that conflict into last-snapshot-wins dispatch.
    protected_argument_candidates: set[str] = field(default_factory=set)
    compatibility_activated: bool = False
    compatibility_activation_reasons: set[str] = field(default_factory=set)
    compatibility_state_chars: int = 0
    protected_argument_state_chars: int = 0
    compatibility_error_codes: set[str] = field(default_factory=set)
    selected_arguments: str | None = None
    selected_argument_source_chars: int | None = None
    argument_diagnostics: dict[str, Any] = field(default_factory=dict)
    fragment_count: int = 0
    name_fragment_count: int = 0
    name_fragment_chars_total: int = 0
    name_fragment_chars_max: int = 0
    name_exact_fragment_count: int = 0
    name_prefix_fragment_count: int = 0
    name_foreign_fragment_count: int = 0
    name_empty_fragment_count: int = 0
    argument_fragment_count: int = 0
    cumulative_argument_snapshots: int = 0
    error_codes: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _ArgumentCandidateResolution:
    selected_arguments: str | None
    selected_argument_source_chars: int | None
    errors: tuple[str, ...]
    diagnostics: dict[str, Any]


class ToolCallStreamAccumulator:
    """Accumulate provider fragments without mixing distinct logical calls.

    Identity rules are intentionally strict:

    * a non-empty provider id always selects (or creates) its own call;
    * ``index`` routes only fragments that do not carry an id;
    * a new id at a reused index starts a new call and changes the active
      continuation target for that index.
    """

    def __init__(self, exposed_tool_names: Iterable[str]) -> None:
        self._exposed_names = frozenset(
            str(name) for name in exposed_tool_names if isinstance(name, str) and name
        )
        self._calls: list[_LogicalCall] = []
        self._id_to_call: dict[str, int] = {}
        self._active_index_to_call: dict[str, int] = {}
        self._global_errors: set[str] = set()
        self.fragment_count = 0
        self.id_routed_fragment_count = 0
        self.index_continuation_count = 0
        self.missing_index_fragment_count = 0
        self.provider_index_reuse_count = 0
        self._argument_candidate_state_chars = 0

    @property
    def logical_call_count(self) -> int:
        return len(self._calls)

    def add_fragment(self, fragment: Mapping[str, Any] | Any) -> None:
        """Add one normalized OpenAI-style ``tool_calls`` delta."""
        self.fragment_count += 1
        if self.fragment_count > _MAX_STREAM_FRAGMENTS:
            self._global_errors.add("tool_fragment_limit_exceeded")
            return
        if not isinstance(fragment, Mapping):
            self._global_errors.add("fragment_not_object")
            return

        provider_id = self._provider_id(fragment)
        index_key = self._index_key(fragment)
        if provider_id is _INVALID or index_key is _INVALID:
            return

        call_index = self._route_call(provider_id, index_key)
        if call_index is None:
            return
        call = self._calls[call_index]
        call.fragment_count += 1

        raw_type = fragment.get("type")
        if raw_type not in (None, "", "function"):
            call.error_codes.add("tool_call_type_invalid")

        function = fragment.get("function") or {}
        if not isinstance(function, Mapping):
            call.error_codes.add("function_not_object")
            return

        if "name" in function and function.get("name") not in (None, ""):
            name_fragment = function.get("name")
            if not isinstance(name_fragment, str):
                call.error_codes.add("name_fragment_not_string")
            else:
                self._merge_name(call, name_fragment)

        if "arguments" in function and function.get("arguments") not in (None, ""):
            arguments_fragment = function.get("arguments")
            if not isinstance(arguments_fragment, str):
                call.error_codes.add("argument_fragment_not_string")
            else:
                self._merge_arguments(call, arguments_fragment)

    def finalize(self, *, iteration: int) -> ToolCallStreamAssembly:
        """Validate atomically while retaining independently complete calls."""
        errors = set(self._global_errors)
        if not self._calls:
            errors.add("missing_tool_calls")

        tentative: list[AssembledStreamToolCall] = []
        complete_calls: list[AssembledStreamToolCall] = []
        used_ids: set[str] = set()
        duplicated_ids: set[str] = set()
        for call in self._calls:
            if call.name_fragment_count == 0:
                name, name_error = None, "missing_tool_name"
            else:
                name, name_error = self._resolved_name(call)
            if name_error:
                call.error_codes.add(name_error)

            argument_resolution = _resolve_call_arguments(call)
            call.selected_arguments = argument_resolution.selected_arguments
            call.selected_argument_source_chars = (
                argument_resolution.selected_argument_source_chars
            )
            call.argument_diagnostics = argument_resolution.diagnostics
            call.error_codes.update(argument_resolution.errors)
            errors.update(call.error_codes)
            raw_arguments = call.selected_arguments or "{}"

            call_id = call.provider_id or f"call_{iteration}_{call.ordinal}"
            if call_id in used_ids:
                errors.add("duplicate_call_id")
                duplicated_ids.add(call_id)
            used_ids.add(call_id)
            tentative.append(AssembledStreamToolCall(
                call_id=call_id,
                name=name or "",
                arguments=raw_arguments,
            ))

        batch_limit_exceeded = bool(
            sum(self._argument_chars(call) for call in self._calls)
            > _MAX_ARGUMENT_CHARS_PER_BATCH
        )
        if batch_limit_exceeded:
            errors.add("tool_argument_batch_limit_exceeded")

        if not self._global_errors and not batch_limit_exceeded:
            for call, assembled in zip(self._calls, tentative):
                if (
                    not call.error_codes
                    and assembled.call_id not in duplicated_ids
                    and assembled.name in self._exposed_names
                    and call.selected_arguments is not None
                ):
                    complete_calls.append(assembled)

        debug = self._debug_summary(errors)
        debug["complete_call_count"] = len(complete_calls)
        return ToolCallStreamAssembly(
            calls=tuple() if errors else tuple(tentative),
            errors=tuple(sorted(errors)),
            debug=debug,
            complete_calls=tuple(complete_calls),
        )

    def debug_summary(self) -> dict[str, Any]:
        """Return current structural state without final JSON validation."""
        errors = set(self._global_errors)
        for call in self._calls:
            errors.update(call.error_codes)
        return self._debug_summary(errors)

    def _new_call(self, provider_id: str | None, index_key: str | None) -> int | None:
        if len(self._calls) >= _MAX_LOGICAL_TOOL_CALLS:
            self._global_errors.add("tool_call_limit_exceeded")
            return None
        call_index = len(self._calls)
        call = _LogicalCall(ordinal=call_index, provider_id=provider_id)
        if index_key is not None:
            call.provider_indexes.add(index_key)
        self._calls.append(call)
        if provider_id is not None:
            self._id_to_call[provider_id] = call_index
        return call_index

    def _route_call(self, provider_id: str | None, index_key: str | None) -> int | None:
        if provider_id is not None:
            self.id_routed_fragment_count += 1
            call_index = self._id_to_call.get(provider_id)
            if call_index is None:
                call_index = self._new_call(provider_id, index_key)
            call = self._calls[call_index]
            if index_key is not None:
                previous = self._active_index_to_call.get(index_key)
                if previous is not None and previous != call_index:
                    self.provider_index_reuse_count += 1
                self._active_index_to_call[index_key] = call_index
                call.provider_indexes.add(index_key)
            else:
                self.missing_index_fragment_count += 1
            return call_index

        if index_key is not None:
            call_index = self._active_index_to_call.get(index_key)
            if call_index is None:
                call_index = self._new_call(None, index_key)
                self._active_index_to_call[index_key] = call_index
            else:
                self.index_continuation_count += 1
            return call_index

        self.missing_index_fragment_count += 1
        if not self._calls:
            return self._new_call(None, None)
        if len(self._calls) == 1:
            return 0
        self._global_errors.add("ambiguous_idless_fragment")
        return None

    def _provider_id(self, fragment: Mapping[str, Any]) -> str | None | object:
        raw_id = fragment.get("id")
        if raw_id in (None, ""):
            return None
        if not isinstance(raw_id, str):
            self._global_errors.add("provider_id_not_string")
            return _INVALID
        return raw_id

    def _index_key(self, fragment: Mapping[str, Any]) -> str | None | object:
        if "index" not in fragment or fragment.get("index") is None:
            return None
        raw_index = fragment.get("index")
        if isinstance(raw_index, bool) or not isinstance(raw_index, (int, str)):
            self._global_errors.add("provider_index_invalid")
            return _INVALID
        return f"{type(raw_index).__name__}:{raw_index}"

    def _merge_name(self, call: _LogicalCall, incoming: str) -> None:
        call.name_fragment_count += 1
        incoming_chars = len(incoming)
        call.name_fragment_chars_total += incoming_chars
        call.name_fragment_chars_max = max(
            call.name_fragment_chars_max,
            incoming_chars,
        )
        if not incoming:
            call.name_empty_fragment_count += 1
        elif incoming in self._exposed_names:
            call.name_exact_fragment_count += 1
        elif any(
            exposed.startswith(incoming)
            for exposed in self._exposed_names
        ):
            call.name_prefix_fragment_count += 1
        else:
            call.name_foreign_fragment_count += 1
        if call.locked_name is not None:
            if incoming != call.locked_name:
                call.error_codes.add("tool_name_conflict")
            return

        next_candidates: set[str] = set()
        for previous in call.name_candidates:
            appended = previous + incoming
            if self._could_be_tool_name(appended):
                next_candidates.add(appended)
            # Providers may emit cumulative snapshots rather than deltas.
            if incoming.startswith(previous) and self._could_be_tool_name(incoming):
                next_candidates.add(incoming)
            if previous.startswith(incoming) and self._could_be_tool_name(previous):
                next_candidates.add(previous)

        if not next_candidates:
            call.error_codes.add("tool_name_conflict")
            return

        call.name_candidates = next_candidates
        if incoming in self._exposed_names and incoming in next_candidates:
            call.locked_name = incoming
            call.name_candidates = {incoming}

    def _could_be_tool_name(self, candidate: str) -> bool:
        return bool(candidate) and any(
            exposed.startswith(candidate) for exposed in self._exposed_names
        )

    def _merge_arguments(self, call: _LogicalCall, incoming: str) -> None:
        call.argument_fragment_count += 1
        if len(incoming) > _MAX_ARGUMENT_CHARS_PER_CALL:
            call.error_codes.add("tool_argument_limit_exceeded")
            return

        standard_before: str | None = None
        snapshot_like = _looks_like_argument_object_start(incoming)
        prefix_related = False
        reset_evidence = False
        if (
            snapshot_like
            and call.standard_argument_parts
            and (
                not call.standard_in_string
                or len(incoming) >= _MIN_SHORTER_SNAPSHOT_PREFIX_CHARS
            )
        ):
            # Materialize only at a possible snapshot boundary.  Ordinary
            # one-character deltas stay O(total argument chars).
            standard_before = "".join(call.standard_argument_parts)
            prefix_related = bool(
                incoming == standard_before
                or incoming.startswith(standard_before)
                or (
                    len(incoming) >= _MIN_SHORTER_SNAPSHOT_PREFIX_CHARS
                    and standard_before.startswith(incoming)
                )
            )
            reset_evidence = not _root_object_is_valid_continuation(
                standard_before
            )

        # Account for the immutable standards path before opening any
        # heuristic branch.  This is a raw retained-state bound, not a final
        # argument-size verdict; cumulative providers may repeat a bounded
        # snapshot several times before one compatibility candidate is chosen.
        if (
            self._argument_candidate_state_chars + len(incoming)
            > _MAX_ARGUMENT_RECONSTRUCTION_STATE_CHARS_PER_BATCH
        ):
            self._global_errors.add("tool_argument_batch_limit_exceeded")
            call.error_codes.add("missing_argument_reconstruction_candidate")
            return
        call.standard_argument_parts.append(incoming)
        call.standard_argument_chars += len(incoming)
        self._argument_candidate_state_chars += len(incoming)
        (
            call.standard_in_string,
            call.standard_trailing_escape,
        ) = _advance_json_string_state(
            incoming,
            in_string=call.standard_in_string,
            escaped=call.standard_trailing_escape,
        )

        if not call.compatibility_activated:
            if not (snapshot_like and (prefix_related or reset_evidence)):
                return
            call.compatibility_activated = True
            if prefix_related:
                call.compatibility_activation_reasons.add(
                    "prefix_related_snapshot"
                )
            if reset_evidence:
                call.compatibility_activation_reasons.add(
                    "grammar_incompatible_root_reset"
                )
            seed_candidates: set[str] = set()
            before = standard_before or ""
            if prefix_related:
                seed_candidates.add(
                    incoming if len(incoming) >= len(before) else before
                )
                # Retain the shorter root snapshot as a separate bounded path.
                # A later suffix can make both interpretations valid, in which
                # case finalization correctly rejects the ambiguity.
                seed_candidates.add(incoming)
                call.cumulative_argument_snapshots += 1
            if reset_evidence:
                seed_candidates.add(incoming)
                self._protect_complete_argument_candidate(call, before)
            self._replace_compatibility_candidates(call, seed_candidates)
            return

        next_candidates: set[str] = set()
        saw_snapshot = False
        for current in call.argument_candidates:
            next_candidates.add(current + incoming)
            if not snapshot_like:
                continue
            related = bool(
                incoming == current
                or incoming.startswith(current)
                or (
                    len(incoming) >= _MIN_SHORTER_SNAPSHOT_PREFIX_CHARS
                    and current.startswith(incoming)
                )
            )
            if related:
                saw_snapshot = True
                next_candidates.add(
                    incoming if len(incoming) >= len(current) else current
                )
                next_candidates.add(incoming)
            elif not _root_object_is_valid_continuation(current):
                self._protect_complete_argument_candidate(call, current)
                next_candidates.add(incoming)
                call.compatibility_activation_reasons.add(
                    "grammar_incompatible_root_reset"
                )
        if saw_snapshot:
            call.cumulative_argument_snapshots += 1
        self._replace_compatibility_candidates(call, next_candidates)

    def _replace_compatibility_candidates(
        self,
        call: _LogicalCall,
        candidates: Iterable[str],
    ) -> None:
        """Install bounded heuristic paths without contaminating standard JSON."""

        base_state = max(
            0,
            self._argument_candidate_state_chars
            - call.compatibility_state_chars,
        )
        next_candidates: set[str] = set()
        state_size = 0
        candidate_overflow = False
        batch_state_overflow = False
        for candidate in sorted(set(candidates), key=lambda item: (len(item), item)):
            if len(candidate) > _MAX_ARGUMENT_CHARS_PER_CALL:
                call.compatibility_error_codes.add(
                    "tool_argument_limit_exceeded"
                )
                continue
            if (
                len(next_candidates)
                + len(call.protected_argument_candidates)
                >= _MAX_ARGUMENT_RECONSTRUCTION_CANDIDATES
            ):
                candidate_overflow = True
                continue
            if (
                call.standard_argument_chars
                + call.protected_argument_state_chars
                + state_size
                + len(candidate)
                > _MAX_ARGUMENT_RECONSTRUCTION_STATE_CHARS
            ):
                candidate_overflow = True
                continue
            if (
                base_state + state_size + len(candidate)
                > _MAX_ARGUMENT_RECONSTRUCTION_STATE_CHARS_PER_BATCH
            ):
                batch_state_overflow = True
                continue
            next_candidates.add(candidate)
            state_size += len(candidate)
        if candidate_overflow:
            call.compatibility_error_codes.add(
                "argument_candidate_limit_exceeded"
            )
        if batch_state_overflow:
            call.compatibility_error_codes.add(
                "tool_argument_batch_limit_exceeded"
            )
        if not next_candidates:
            call.compatibility_error_codes.add(
                "missing_argument_reconstruction_candidate"
            )
        call.argument_candidates = next_candidates
        call.compatibility_state_chars = state_size
        self._argument_candidate_state_chars = base_state + state_size

    def _protect_complete_argument_candidate(
        self,
        call: _LogicalCall,
        candidate: str,
    ) -> None:
        """Retain a complete pre-reset value as an immutable conflict path."""

        if (
            not candidate
            or candidate in call.protected_argument_candidates
            or not _is_complete_argument_object(candidate)
        ):
            return
        if (
            len(call.protected_argument_candidates)
            + len(call.argument_candidates)
            >= _MAX_ARGUMENT_RECONSTRUCTION_CANDIDATES
        ):
            call.compatibility_error_codes.add(
                "argument_candidate_limit_exceeded"
            )
            return
        candidate_chars = len(candidate)
        if (
            call.standard_argument_chars
            + call.compatibility_state_chars
            + call.protected_argument_state_chars
            + candidate_chars
            > _MAX_ARGUMENT_RECONSTRUCTION_STATE_CHARS
        ):
            call.compatibility_error_codes.add(
                "argument_candidate_limit_exceeded"
            )
            return
        if (
            self._argument_candidate_state_chars + candidate_chars
            > _MAX_ARGUMENT_RECONSTRUCTION_STATE_CHARS_PER_BATCH
        ):
            call.compatibility_error_codes.add(
                "tool_argument_batch_limit_exceeded"
            )
            return
        call.protected_argument_candidates.add(candidate)
        call.protected_argument_state_chars += candidate_chars
        self._argument_candidate_state_chars += candidate_chars

    def _resolved_name(self, call: _LogicalCall) -> tuple[str | None, str | None]:
        if call.locked_name is not None:
            return call.locked_name, None
        # End-of-stream is a trust boundary.  A name assembled from multiple
        # fragments may be accepted only after those fragments spell an exact
        # exposed tool name.  Completing a unique prefix here (for example
        # ``w`` -> ``write_file``) would turn a truncated/corrupt stream into a
        # side-effecting call instead of routing it through recovery.
        exact_matches = {
            candidate
            for candidate in call.name_candidates
            if candidate in self._exposed_names
        }
        if len(exact_matches) == 1:
            return next(iter(exact_matches)), None
        if not call.name_candidates or not any(call.name_candidates):
            return None, "tool_name_unrecognized"
        if any(
            exposed.startswith(candidate)
            for candidate in call.name_candidates
            if candidate
            for exposed in self._exposed_names
        ):
            return None, "tool_name_incomplete"
        return None, "tool_name_unrecognized"

    def _debug_summary(self, errors: set[str]) -> dict[str, Any]:
        return {
            "fragment_count": self.fragment_count,
            "logical_call_count": len(self._calls),
            "exposed_tool_count": len(self._exposed_names),
            "id_routed_fragment_count": self.id_routed_fragment_count,
            "index_continuation_count": self.index_continuation_count,
            "missing_index_fragment_count": self.missing_index_fragment_count,
            "provider_index_reuse_count": self.provider_index_reuse_count,
            "argument_chars_total": sum(
                self._argument_chars(call) for call in self._calls
            ),
            "argument_candidate_state_chars_total": (
                self._argument_candidate_state_chars
            ),
            "cumulative_argument_snapshot_count": sum(
                call.cumulative_argument_snapshots for call in self._calls
            ),
            "corrupt": bool(errors),
            "error_codes": sorted(errors),
            "calls": [
                {
                    "ordinal": call.ordinal,
                    "has_provider_id": call.provider_id is not None,
                    "provider_index_count": len(call.provider_indexes),
                    "fragment_count": call.fragment_count,
                    "name_fragment_count": call.name_fragment_count,
                    "name_fragment_chars_total": (
                        call.name_fragment_chars_total
                    ),
                    "name_fragment_chars_max": (
                        call.name_fragment_chars_max
                    ),
                    "name_relation_counts": {
                        "exact_exposed": (
                            call.name_exact_fragment_count
                        ),
                        "exposed_prefix": (
                            call.name_prefix_fragment_count
                        ),
                        "foreign": call.name_foreign_fragment_count,
                        "empty": call.name_empty_fragment_count,
                    },
                    "name_resolution": self._debug_name_resolution(call),
                    "argument_fragment_count": call.argument_fragment_count,
                    "argument_chars": self._argument_chars(call),
                    "argument_candidate_count": (
                        1
                        + len(call.argument_candidates)
                        + len(call.protected_argument_candidates)
                        if call.standard_argument_parts
                        else max(
                            1,
                            len(call.argument_candidates)
                            + len(call.protected_argument_candidates),
                        )
                    ),
                    "standard_argument_chars": call.standard_argument_chars,
                    "compatibility_activated": call.compatibility_activated,
                    "compatibility_activation_reasons": sorted(
                        call.compatibility_activation_reasons
                    ),
                    "compatibility_candidate_count": len(
                        call.argument_candidates
                    ),
                    "protected_argument_candidate_count": len(
                        call.protected_argument_candidates
                    ),
                    "compatibility_state_chars": (
                        call.compatibility_state_chars
                    ),
                    "protected_argument_state_chars": (
                        call.protected_argument_state_chars
                    ),
                    "compatibility_error_codes": sorted(
                        call.compatibility_error_codes
                    ),
                    "cumulative_argument_snapshots": call.cumulative_argument_snapshots,
                    "corrupt": bool(call.error_codes),
                    "error_codes": sorted(call.error_codes),
                    "argument_json": dict(call.argument_diagnostics),
                }
                for call in self._calls
            ],
        }

    def _debug_name_resolution(self, call: _LogicalCall) -> str:
        """Return a payload-free relation between observed and exposed names."""

        if call.locked_name in self._exposed_names:
            return "exact_exposed"
        if any(
            candidate in self._exposed_names
            for candidate in call.name_candidates
        ):
            return "exact_exposed"
        if call.name_fragment_count == 0:
            return "missing"
        if any(candidate for candidate in call.name_candidates):
            return "exposed_prefix"
        if call.name_foreign_fragment_count:
            return "foreign_or_conflict"
        if "tool_name_conflict" in call.error_codes:
            return "conflict"
        return "unresolved"

    @staticmethod
    def _argument_chars(call: _LogicalCall) -> int:
        if call.selected_argument_source_chars is not None:
            return call.selected_argument_source_chars
        return max(
            call.standard_argument_chars,
            max((len(item) for item in call.argument_candidates), default=0),
            max(
                (len(item) for item in call.protected_argument_candidates),
                default=0,
            ),
        )


def _looks_like_argument_object_start(fragment: str) -> bool:
    return fragment.lstrip().startswith("{")


def _is_complete_argument_object(candidate: str) -> bool:
    """Whether one retained snapshot is a complete, bounded JSON object."""

    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return False
    return bool(
        isinstance(parsed, dict)
        and json_argument_structure_error(parsed) is None
    )


def _advance_json_string_state(
    fragment: str,
    *,
    in_string: bool,
    escaped: bool,
) -> tuple[bool, bool]:
    """Advance only JSON quote/escape state for the exact standard bytes."""

    for char in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
    return in_string, escaped


def _root_object_is_valid_continuation(prefix: str) -> bool:
    """Whether a root ``{`` can be an ordinary next JSON delta.

    This is deliberately a lexical/structural predicate, not a permissive JSON
    repair parser.  It exists only to decide whether to open a bounded snapshot
    compatibility path.  The exact standards path is retained regardless and
    must still parse as one complete JSON object at finalization.
    """

    stack: list[str] = []
    in_string = False
    escaped = False
    last_significant = ""
    structurally_invalid = False
    for char in prefix:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                last_significant = '"'
            continue
        if char.isspace():
            continue
        last_significant = char
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("object")
        elif char == "[":
            stack.append("array")
        elif char == "}":
            if not stack or stack[-1] != "object":
                structurally_invalid = True
            else:
                stack.pop()
        elif char == "]":
            if not stack or stack[-1] != "array":
                structurally_invalid = True
            else:
                stack.pop()

    if structurally_invalid:
        return False
    if in_string:
        # A leading brace in the next chunk is ordinary string content.  A
        # prefix-related full snapshot is detected separately and may still
        # open the compatibility path.
        return True
    if not last_significant:
        return True
    if last_significant in {":", "["}:
        return True
    if last_significant == "," and stack and stack[-1] == "array":
        return True
    return False


def _resolve_call_arguments(call: _LogicalCall) -> _ArgumentCandidateResolution:
    """Prefer exact standard deltas; consult snapshot heuristics only on failure."""

    standard_raw = (
        "".join(call.standard_argument_parts)
        if call.standard_argument_parts
        else "{}"
    )
    standard = _resolve_argument_candidates((standard_raw,))
    if standard.selected_arguments is not None and not standard.errors:
        diagnostics = {
            **standard.diagnostics,
            "selected_argument_mode": "standard_delta",
            "standard_chars": len(standard_raw),
            "compatibility_activated": call.compatibility_activated,
            "compatibility_candidate_count": len(call.argument_candidates),
            "compatibility_errors_ignored": sorted(
                call.compatibility_error_codes
            ),
        }
        return _ArgumentCandidateResolution(
            selected_arguments=standard.selected_arguments,
            selected_argument_source_chars=(
                standard.selected_argument_source_chars
            ),
            errors=(),
            diagnostics=diagnostics,
        )

    compatibility_inputs = (
        call.argument_candidates | call.protected_argument_candidates
        if call.compatibility_activated
        and (
            call.argument_candidates
            or call.protected_argument_candidates
        )
        else {standard_raw}
    )
    compatibility = _resolve_argument_candidates(compatibility_inputs)
    compatibility_errors = set(compatibility.errors)
    compatibility_errors.update(call.compatibility_error_codes)
    selected_arguments = compatibility.selected_arguments
    selected_source_chars = compatibility.selected_argument_source_chars
    if compatibility_errors:
        selected_arguments = None
        selected_source_chars = None
    diagnostics = {
        **compatibility.diagnostics,
        "selected_argument_mode": (
            "snapshot_compatibility"
            if selected_arguments is not None
            else "none"
        ),
        "standard_chars": len(standard_raw),
        "standard_error_codes": list(standard.errors),
        "compatibility_activated": call.compatibility_activated,
        "compatibility_candidate_count": len(call.argument_candidates),
        "compatibility_error_codes": sorted(
            call.compatibility_error_codes
        ),
    }
    return _ArgumentCandidateResolution(
        selected_arguments=selected_arguments,
        selected_argument_source_chars=selected_source_chars,
        errors=tuple(sorted(compatibility_errors)),
        diagnostics=diagnostics,
    )


def _resolve_argument_candidates(
    candidates: Iterable[str],
) -> _ArgumentCandidateResolution:
    """Select exactly one semantic JSON object from bounded reconstructions.

    Invalid candidates do not contaminate a unique valid reconstruction. Two
    differently formatted candidates that decode to the same object are one
    semantic candidate; two distinct objects are an ambiguity and therefore
    cannot cross the dispatch trust boundary.
    """
    unique_candidates: list[str] = []
    seen_candidates: set[str] = set()
    errors: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            errors.add("argument_candidate_not_string")
            continue
        if candidate in seen_candidates:
            continue
        seen_candidates.add(candidate)
        if len(candidate) > _MAX_ARGUMENT_CHARS_PER_CALL:
            errors.add("tool_argument_limit_exceeded")
            continue
        if len(unique_candidates) >= _MAX_ARGUMENT_RECONSTRUCTION_CANDIDATES:
            errors.add("argument_candidate_limit_exceeded")
            continue
        unique_candidates.append(candidate)

    if not unique_candidates:
        errors.add("missing_argument_reconstruction_candidate")
    unique_candidates.sort(key=lambda item: (len(item), item))

    semantic_objects: dict[str, str] = {}
    semantic_source_chars: dict[str, int] = {}
    valid_candidate_count = 0
    malformed_count = 0
    non_object_count = 0
    structure_errors: set[str] = set()
    parse_error_positions: set[int] = set()
    profiles: list[dict[str, Any]] = []

    for candidate in unique_candidates:
        if len(profiles) < _MAX_ARGUMENT_DIAGNOSTIC_PROFILES:
            profiles.append(_argument_boundary_profile(candidate))
        try:
            parsed_arguments = json.loads(candidate)
        except json.JSONDecodeError as exc:
            malformed_count += 1
            parse_error_positions.add(int(exc.pos))
            continue
        except (TypeError, ValueError, RecursionError):
            malformed_count += 1
            continue
        if not isinstance(parsed_arguments, dict):
            non_object_count += 1
            continue
        structure_error = json_argument_structure_error(parsed_arguments)
        if structure_error:
            structure_errors.add(structure_error)
            continue

        valid_candidate_count += 1
        canonical = json.dumps(
            parsed_arguments,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        semantic_objects.setdefault(canonical, canonical)
        semantic_source_chars[canonical] = max(
            semantic_source_chars.get(canonical, 0),
            len(candidate),
        )

    selected_arguments: str | None = None
    selected_argument_source_chars: int | None = None
    if len(semantic_objects) == 1 and not errors:
        semantic_key, selected_arguments = next(iter(semantic_objects.items()))
        selected_argument_source_chars = semantic_source_chars[semantic_key]
    elif len(semantic_objects) > 1:
        errors.add("ambiguous_arguments_json")
    else:
        if malformed_count:
            errors.add("malformed_arguments_json")
        if non_object_count:
            errors.add("arguments_not_object")
        errors.update(structure_errors)

    diagnostics = {
        "candidate_count": len(unique_candidates),
        "valid_object_candidate_count": valid_candidate_count,
        "valid_semantic_object_count": len(semantic_objects),
        "malformed_candidate_count": malformed_count,
        "non_object_candidate_count": non_object_count,
        "parse_error_positions": sorted(parse_error_positions)[
            :_MAX_ARGUMENT_DIAGNOSTIC_PROFILES
        ],
        "profile_count": len(profiles),
        "profiles_truncated": max(0, len(unique_candidates) - len(profiles)),
        "profiles": profiles,
    }
    return _ArgumentCandidateResolution(
        selected_arguments=selected_arguments,
        selected_argument_source_chars=selected_argument_source_chars,
        errors=tuple(sorted(errors)),
        diagnostics=diagnostics,
    )


def _argument_boundary_profile(candidate: str) -> dict[str, Any]:
    """Return payload-free JSON boundary diagnostics for one candidate."""
    first = next((char for char in candidate if not char.isspace()), "")
    last = next((char for char in reversed(candidate) if not char.isspace()), "")
    object_balance = 0
    array_balance = 0
    minimum_object_balance = 0
    minimum_array_balance = 0
    in_string = False
    escaped = False
    for char in candidate:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            object_balance += 1
        elif char == "}":
            object_balance -= 1
            minimum_object_balance = min(minimum_object_balance, object_balance)
        elif char == "[":
            array_balance += 1
        elif char == "]":
            array_balance -= 1
            minimum_array_balance = min(minimum_array_balance, array_balance)
    return {
        "chars": len(candidate),
        "first_token_class": _boundary_character_class(first),
        "last_token_class": _boundary_character_class(last),
        "object_balance": object_balance,
        "array_balance": array_balance,
        "minimum_object_balance": minimum_object_balance,
        "minimum_array_balance": minimum_array_balance,
        "in_string": in_string,
        "trailing_escape": escaped,
    }


def _boundary_character_class(char: str) -> str:
    if not char:
        return "empty"
    if char == "{":
        return "object_open"
    if char == "}":
        return "object_close"
    if char == "[":
        return "array_open"
    if char == "]":
        return "array_close"
    if char == '"':
        return "quote"
    if char.isdigit() or char == "-":
        return "number"
    if char.isalpha():
        return "alpha"
    return "other"


def validate_nonstream_tool_call_batch(
    payload: Mapping[str, Any] | Any,
    exposed_tool_names: Iterable[str],
) -> ToolCallStreamAssembly:
    """Strictly validate one OpenAI-compatible non-stream fallback response.

    The fallback is deliberately narrower than the normal transport parser:
    it accepts only ``choices[0].message.tool_calls`` with non-empty unique
    ids, an exactly exposed function name, and arguments that decode to a JSON
    object.  Validation is atomic, and its debug data never contains provider
    ids, tool names, or argument values.
    """
    exposed_names = frozenset(
        str(name) for name in exposed_tool_names
        if isinstance(name, str) and name
    )
    errors: set[str] = set()
    choice_count = 0
    raw_calls: Any = None

    if not isinstance(payload, Mapping):
        errors.add("fallback_response_not_object")
    else:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            errors.add("fallback_choices_missing")
        else:
            choice_count = len(choices)
            first_choice = choices[0]
            if not isinstance(first_choice, Mapping):
                errors.add("fallback_choice_not_object")
            else:
                message = first_choice.get("message")
                if not isinstance(message, Mapping):
                    errors.add("fallback_message_not_object")
                else:
                    raw_calls = message.get("tool_calls")
                    if not isinstance(raw_calls, list) or not raw_calls:
                        errors.add("fallback_tool_calls_missing")

    tentative: list[AssembledStreamToolCall] = []
    complete_calls: list[AssembledStreamToolCall] = []
    per_call_errors: list[set[str]] = []
    used_ids: set[str] = set()
    duplicated_ids: set[str] = set()
    call_debug: list[dict[str, Any]] = []
    envelope_errors = set(errors)
    call_limit_exceeded = bool(
        isinstance(raw_calls, list)
        and len(raw_calls) > _MAX_LOGICAL_TOOL_CALLS
    )
    if isinstance(raw_calls, list):
        if call_limit_exceeded:
            errors.add("fallback_tool_call_limit_exceeded")
        for ordinal, raw_call in enumerate(raw_calls):
            call_errors: set[str] = set()
            call_id = ""
            name = ""
            raw_arguments = ""
            name_chars = 0
            name_relation = "missing"

            if not isinstance(raw_call, Mapping):
                call_errors.add("fallback_tool_call_not_object")
                name_relation = "call_not_object"
            else:
                raw_id = raw_call.get("id")
                if not isinstance(raw_id, str) or not raw_id.strip():
                    call_errors.add("fallback_tool_call_id_missing")
                else:
                    call_id = raw_id
                    if call_id in used_ids:
                        call_errors.add("fallback_duplicate_tool_call_id")
                        duplicated_ids.add(call_id)
                    used_ids.add(call_id)

                raw_type = raw_call.get("type", "function")
                if raw_type != "function":
                    call_errors.add("fallback_tool_call_type_invalid")

                function = raw_call.get("function")
                if not isinstance(function, Mapping):
                    call_errors.add("fallback_function_not_object")
                    name_relation = "function_not_object"
                else:
                    raw_name = function.get("name")
                    if not isinstance(raw_name, str) or not raw_name:
                        call_errors.add("fallback_tool_name_missing")
                        name_relation = (
                            "not_string"
                            if raw_name is not None
                            and not isinstance(raw_name, str)
                            else "missing"
                        )
                    else:
                        name = raw_name
                        name_chars = len(raw_name)
                        if name not in exposed_names:
                            call_errors.add("fallback_tool_name_not_exposed")
                            name_relation = (
                                "exposed_prefix"
                                if any(
                                    exposed.startswith(name)
                                    for exposed in exposed_names
                                )
                                else "foreign"
                            )
                        else:
                            name_relation = "exact_exposed"

                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        call_errors.add("fallback_arguments_not_string")
                    else:
                        raw_arguments = arguments
                        if len(arguments) > _MAX_ARGUMENT_CHARS_PER_CALL:
                            call_errors.add("fallback_argument_limit_exceeded")
                        try:
                            parsed_arguments = json.loads(arguments)
                        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
                            call_errors.add("fallback_arguments_malformed_json")
                        else:
                            if not isinstance(parsed_arguments, dict):
                                call_errors.add("fallback_arguments_not_object")
                            else:
                                structure_error = json_argument_structure_error(
                                    parsed_arguments
                                )
                                if structure_error:
                                    call_errors.add("fallback_" + structure_error)

            errors.update(call_errors)
            per_call_errors.append(call_errors)
            call_debug.append({
                "ordinal": ordinal,
                "has_provider_id": bool(call_id),
                "name_chars": name_chars,
                "name_relation": name_relation,
                "argument_chars": len(raw_arguments),
                "corrupt": bool(call_errors),
                "error_codes": sorted(call_errors),
            })
            tentative.append(AssembledStreamToolCall(
                call_id=call_id,
                name=name,
                arguments=raw_arguments,
            ))

    batch_limit_exceeded = bool(
        sum(len(call.arguments) for call in tentative)
        > _MAX_ARGUMENT_CHARS_PER_BATCH
    )
    if batch_limit_exceeded:
        errors.add("fallback_argument_batch_limit_exceeded")

    if not envelope_errors and not call_limit_exceeded and not batch_limit_exceeded:
        for assembled, call_errors in zip(tentative, per_call_errors):
            if not call_errors and assembled.call_id not in duplicated_ids:
                complete_calls.append(assembled)

    debug = {
        "choice_count": choice_count,
        "tool_call_count": len(raw_calls) if isinstance(raw_calls, list) else 0,
        "exposed_tool_count": len(exposed_names),
        "argument_chars_total": sum(
            int(item.get("argument_chars") or 0) for item in call_debug
        ),
        "corrupt": bool(errors),
        "error_codes": sorted(errors),
        "complete_call_count": len(complete_calls),
        "calls": call_debug,
    }
    return ToolCallStreamAssembly(
        calls=tuple() if errors else tuple(tentative),
        errors=tuple(sorted(errors)),
        debug=debug,
        complete_calls=tuple(complete_calls),
    )


_INVALID = object()
