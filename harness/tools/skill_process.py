"""Persistent, exact-Skill process sessions owned by the Harness runtime.

The model-facing surface is deliberately one compact stateful tool.  A model
may choose an exact, already-compiled Skill script on ``start`` and may then
operate only the opaque ``process_id`` returned by this module.  Executor
socket selection, process-lease handles, owner authority, content digests, and
idempotency operation IDs never enter the public schema.

This is a capability adapter, not a general shell.  It snapshots one verified
Skill package and the current session workspace into an isolated executor.
Browser automation is selected only from machine-readable declarations or
exact source imports and fails closed when its dedicated runtime is absent.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import secrets
import uuid
from typing import Any

from tools.context import ToolContext
from tools.execution_fence import (
    ExecutionAuthorityRevoked,
    require_execution_authority,
)
from tools.isolated_skill_executor import (
    MAX_ARGS,
    MAX_ARG_CHARS,
    MAX_FUNCTION_ARGS,
    MAX_FUNCTION_KWARGS,
    MAX_PROCESS_CALL_BYTES,
    MAX_PROCESS_LEASE_TTL_SECONDS,
    MAX_PROCESS_READ_BYTES,
    MAX_PROCESS_READ_WAIT_MS,
    MAX_PROCESS_RUNTIME_SECONDS,
    MAX_PROCESS_STDIN_CHUNK_BYTES,
    SkillPackageSnapshot,
    IsolatedProcessLease,
    IsolatedSkillExecutorError,
    ProcessOwnerScope,
    call_isolated_process_instance,
    close_isolated_process_stdin,
    close_isolated_process_lease,
    create_process_owner_scope,
    finalize_terminal_process_lease_error,
    open_isolated_process_lease,
    read_isolated_process_output,
    signal_isolated_process,
    start_isolated_process_lease,
    sync_isolated_process_artifacts,
    terminal_process_lease_error_action,
    write_isolated_process_stdin,
    snapshot_skill_package,
)
from tools.path_security import sandbox_dir
from tools.session_sandbox_policy import (
    SessionSandboxPolicyError,
    session_sandbox_egress_budget_binding,
)
from tools.skill_invocation_egress import (
    bind_python_invocation_parameters,
    invocation_bound_skill_egress_policy_for_invocations,
)
from skill_capability_plan import (
    build_callable_skill_result_receipt,
    build_skill_process_evidence_receipt,
    callable_skill_result_receipt_is_failure,
    skill_process_artifact_manifest_sha256,
)
from tools.skill_script import (
    SkillScriptError,
    _display_script_path,
    _resolve_session_skill_script,
)
from tools.skill_runtime_profile import (
    runtime_profile_socket_binding,
    select_skill_runtime_profile,
)


MAX_SCRIPT_PATH_CHARS = 2_048
MAX_PROCESS_ID_CHARS = 128
MAX_PUBLIC_RESULT_TEXT_CHARS = 262_144
MAX_PROCESS_RECEIPT_LINE_BYTES = 256 * 1024
MAX_PENDING_START_CLEANUP_WAIT_SECONDS = 45.0
MAX_PROCESS_CLOSE_CLEANUP_WAIT_SECONDS = 60.0
# A provider turn can legitimately spend well over five minutes reasoning
# between two commands of the same browser/CLI session.  Keep the default
# lease alive for the executor's full bounded hour; root-run, session, and
# Harness-shutdown cleanup still close abandoned processes eagerly.
DEFAULT_IDLE_TTL_SECONDS = MAX_PROCESS_LEASE_TTL_SECONDS
DEFAULT_MAX_RUNTIME_SECONDS = MAX_PROCESS_RUNTIME_SECONDS

_PUBLIC_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_PROCESS_ID_RE = re.compile(r"^sp_[A-Za-z0-9_-]{24,96}$")
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_FIELDS: dict[str, frozenset[str]] = {
    "start": frozenset({
        "operation",
        "script_path",
        "args",
        "cwd",
        "idle_ttl_seconds",
        "max_runtime_seconds",
        "class_name",
        "factory_name",
        "constructor_args",
        "constructor_kwargs",
    }),
    "write": frozenset({
        "operation",
        "process_id",
        "data",
        "data_base64",
    }),
    "stdin_close": frozenset({"operation", "process_id"}),
    "read": frozenset({
        "operation",
        "process_id",
        "stdout_offset",
        "stderr_offset",
        "max_bytes",
        "wait_ms",
    }),
    "call": frozenset({
        "operation",
        "process_id",
        "method_name",
        "method_args",
        "method_kwargs",
    }),
    "sync": frozenset({"operation", "process_id"}),
    "signal": frozenset({"operation", "process_id", "signal"}),
    "close": frozenset({"operation", "process_id"}),
}


def _json_error(code: str, message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {
        "status": "error",
        "error_code": code,
        "error": message,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def preflight_run_skill_process_args(
    args: dict[str, Any],
    context: ToolContext | None,
) -> dict[str, Any] | None:
    """Pure operation-shape gate before any stateful handler dispatch."""

    del context
    operation = args.get("operation")
    allowed = _OPERATION_FIELDS.get(str(operation or ""))
    if allowed is None:
        return {
            "error": (
                "operation must be start, write, stdin_close, read, call, "
                "sync, signal, or close."
            ),
            "reason": "invalid_process_operation",
        }
    unexpected = sorted(set(args) - allowed)
    if unexpected:
        return {
            "error": (
                f"{operation} does not accept fields: "
                + ", ".join(unexpected)
            ),
            "reason": "invalid_process_operation_fields",
        }
    if operation == "start":
        if not args.get("script_path"):
            return {
                "error": "start requires script_path.",
                "reason": "missing_process_script_path",
            }
        if args.get("class_name") and args.get("factory_name"):
            return {
                "error": "class_name and factory_name are mutually exclusive.",
                "reason": "invalid_process_invocation",
            }
        if (
            (args.get("class_name") or args.get("factory_name"))
            and args.get("args")
        ):
            return {
                "error": (
                    "Structured class/factory sessions do not accept CLI args."
                ),
                "reason": "invalid_process_invocation",
            }
        if (
            ("constructor_args" in args or "constructor_kwargs" in args)
            and not (args.get("class_name") or args.get("factory_name"))
        ):
            return {
                "error": (
                    "constructor arguments require class_name or factory_name."
                ),
                "reason": "invalid_process_invocation",
            }
        return None
    if not args.get("process_id"):
        return {
            "error": f"{operation} requires process_id returned by start.",
            "reason": "missing_process_id",
        }
    if operation == "write" and (
        ("data" in args) == ("data_base64" in args)
    ):
        return {
            "error": "write requires exactly one of data or data_base64.",
            "reason": "invalid_stdin",
        }
    if operation == "call" and not args.get("method_name"):
        return {
            "error": "call requires method_name.",
            "reason": "invalid_function_call",
        }
    if operation == "signal" and not args.get("signal"):
        return {
            "error": "signal requires signal.",
            "reason": "invalid_signal",
        }
    return None


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and _HEX_SHA256_RE.fullmatch(value) is not None


def _owner_key(context: ToolContext) -> tuple[str, str, str]:
    root_run_id = str(
        context.root_run_id
        or context.run_id
        or context.browser_run_scope_id
        or ""
    )
    if not root_run_id:
        raise IsolatedSkillExecutorError(
            "missing_process_owner",
            "Persistent Skill execution requires a runtime-owned run scope.",
        )
    return (str(context.user_id), str(context.session_id), root_run_id)


def _operation_uuid(context: ToolContext, operation: str) -> str:
    """Derive one stable protocol UUID without accepting a model field."""

    raw = str(getattr(context, "tool_operation_id", "") or "")
    try:
        namespace = uuid.UUID(raw)
    except (ValueError, AttributeError):
        # Direct internal callers which bypass AgentLoop still receive a
        # runtime-generated id.  Production model dispatch always supplies the
        # stable tool-call-derived value through ToolContext.
        namespace = uuid.uuid4()
    return str(uuid.uuid5(namespace, f"run_skill_process:{operation}"))


def _safe_public_identifier(
    value: Any,
    *,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or value.startswith("_")
        or _PUBLIC_IDENTIFIER_RE.fullmatch(value) is None
    ):
        raise IsolatedSkillExecutorError(
            "invalid_process_invocation",
            f"{field_name} must be one public, non-dotted identifier.",
        )
    return value


def _declared_public_python_symbols(
    source: str,
) -> tuple[set[str], set[str]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_python_entrypoint",
            "Structured persistent invocation requires a parseable Python script.",
        ) from exc
    classes = {
        node.name
        for node in tree.body
        if (
            isinstance(node, ast.ClassDef)
            and not node.name.startswith("_")
            and not node.decorator_list
        )
    }
    factories = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
        and not node.decorator_list
    }
    return classes, factories


def _runtime_profile_for_script(script: Path, skill_root: Path) -> str:
    """Compatibility wrapper over the immutable snapshot selector."""

    try:
        relative = script.resolve(strict=True).relative_to(
            skill_root.resolve(strict=True)
        ).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise IsolatedSkillExecutorError(
            "skill_runtime_profile_unavailable",
            "The exact Skill entrypoint cannot be resolved safely.",
        ) from exc
    snapshot = snapshot_skill_package(skill_root)
    return select_skill_runtime_profile(snapshot, relative).runtime_profile


def _executor_socket_for_profile(profile: str) -> str:
    return runtime_profile_socket_binding(profile).socket_path


@dataclass(frozen=True, slots=True)
class _VerifiedScriptAuthority:
    skill_name: str
    script_resource: str
    script_sha256: str
    root_sha256: str
    declaring_resource: str
    declaring_sha256: str
    package_sha256: str
    script: Path
    skill_root: Path
    snapshot: SkillPackageSnapshot = field(repr=False)

    @property
    def row(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.skill_name,
            self.root_sha256,
            self.declaring_resource,
            self.declaring_sha256,
            self.script_resource,
            self.script_sha256,
        )

    @property
    def package_row(self) -> tuple[str, str]:
        return (self.skill_name, self.package_sha256)


def _verify_exact_script_authority(
    script_path: str,
    context: ToolContext,
) -> _VerifiedScriptAuthority:
    if (
        not isinstance(script_path, str)
        or not script_path.startswith("skills/")
        or len(script_path) > MAX_SCRIPT_PATH_CHARS
        or script_path != script_path.strip()
    ):
        raise IsolatedSkillExecutorError(
            "invalid_script_path",
            "start requires the full exact path "
            "skills/<canonical-skill>/<relative-script>.",
        )
    try:
        script, skill_root, skill_name = _resolve_session_skill_script(
            script_path,
            context.user_id,
            context.session_id,
            list(context.enabled_user_skills),
        )
    except SkillScriptError as exc:
        raise IsolatedSkillExecutorError(exc.code, str(exc)) from exc
    canonical_path = _display_script_path(script, skill_root, skill_name)
    if canonical_path != script_path:
        raise IsolatedSkillExecutorError(
            "noncanonical_script_path",
            f"Use the exact canonical Skill path: {canonical_path}.",
        )
    script_resource = script.relative_to(skill_root).as_posix()
    snapshot = snapshot_skill_package(skill_root)
    try:
        script_digest = snapshot.file_sha256(script_resource)
    except KeyError as exc:
        raise IsolatedSkillExecutorError(
            "skill_script_unavailable",
            "The exact authorized Skill script is unavailable.",
        ) from exc
    if (
        skill_name,
        script_resource,
        script_digest,
    ) not in set(context.allowed_skill_scripts):
        raise IsolatedSkillExecutorError(
            "skill_script_authority_mismatch",
            "The exact Skill script and its current digest are not authorized "
            "by this compiled run.",
        )

    try:
        root_digest = snapshot.file_sha256("SKILL.md")
        package_digest = snapshot.sha256
    except KeyError as exc:
        raise IsolatedSkillExecutorError(
            "skill_package_authority_mismatch",
            "The complete Skill package cannot be safely verified.",
        ) from exc
    if (
        skill_name,
        package_digest,
    ) not in set(context.allowed_skill_package_digests):
        raise IsolatedSkillExecutorError(
            "skill_package_authority_mismatch",
            "The complete current Skill package is not authorized by this "
            "compiled run. Re-read and recompile the exact Skill.",
        )
    for row in context.allowed_skill_script_authorities:
        if (
            len(row) != 6
            or row[0] != skill_name
            or row[1] != root_digest
            or row[4] != script_resource
            or row[5] != script_digest
            or not _valid_digest(row[3])
        ):
            continue
        try:
            declaration_digest = snapshot.file_sha256(row[2])
        except KeyError:
            continue
        if declaration_digest != row[3]:
            continue
        return _VerifiedScriptAuthority(
            skill_name=skill_name,
            script_resource=script_resource,
            script_sha256=script_digest,
            root_sha256=root_digest,
            declaring_resource=row[2],
            declaring_sha256=declaration_digest,
            package_sha256=package_digest,
            script=script,
            skill_root=skill_root,
            snapshot=snapshot,
        )
    raise IsolatedSkillExecutorError(
        "skill_script_authority_mismatch",
        "The Skill root/reference/script digest chain is missing or changed. "
        "Re-read and recompile the exact Skill before starting it.",
    )


def _context_permits_record(
    context: ToolContext,
    authority: _VerifiedScriptAuthority,
) -> bool:
    if (
        authority.skill_name,
        authority.script_resource,
        authority.script_sha256,
    ) not in set(context.allowed_skill_scripts):
        return False
    return (
        authority.row in set(context.allowed_skill_script_authorities)
        and authority.package_row in set(
            context.allowed_skill_package_digests
        )
    )


@dataclass(slots=True)
class _PendingStructuredCall:
    call_id: str
    method_name: str
    semantic_task_bound: bool


@dataclass(slots=True)
class _PendingProcessStart:
    pending_id: str
    owner: tuple[str, str, str]
    execution_run_id: str
    owner_generation: int
    owner_scope: ProcessOwnerScope
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    admission_cancel_event: asyncio.Event = field(
        default_factory=asyncio.Event,
        repr=False,
    )
    cancel_requested: bool = False


@dataclass(frozen=True, slots=True)
class _ActiveProcessCleanup:
    cleanup_id: str
    predicate: Any = field(repr=False)
    execution_run: tuple[str, str, str] | None = None

    def matches(
        self,
        owner: tuple[str, str, str],
        execution_run_id: str,
    ) -> bool:
        if self.execution_run is None:
            return bool(self.predicate(owner))
        return (
            owner[:2] == self.execution_run[:2]
            and execution_run_id == self.execution_run[2]
        )


@dataclass(frozen=True, slots=True)
class _MutationFence:
    operation: str
    operation_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _CloseTransactionResult:
    response: dict[str, Any]
    evidence_receipt: dict[str, Any] | None


@dataclass(slots=True)
class _ManagedProcess:
    process_id: str
    owner: tuple[str, str, str]
    lease: IsolatedProcessLease
    authority: _VerifiedScriptAuthority
    canonical_script_path: str
    runtime_profile: str
    execution_run_id: str
    cli_semantic_task_bound: bool = False
    cli_stdout_observed_offset: int = 0
    cli_stdout_size_bytes: int = 0
    cli_stdout_hasher: Any = field(
        default_factory=hashlib.sha256,
        repr=False,
    )
    cli_stdout_observation_failed: bool = False
    stdout_parse_offset: int = 0
    stdout_line_fragment: bytearray = field(default_factory=bytearray, repr=False)
    stdout_receipt_parsing_failed: bool = False
    pending_structured_calls: dict[str, _PendingStructuredCall] = field(
        default_factory=dict,
        repr=False,
    )
    structured_call_history: dict[str, _PendingStructuredCall] = field(
        default_factory=dict,
        repr=False,
    )
    latest_semantic_call_id: str | None = None
    completed_process_receipts: list[dict[str, Any]] = field(
        default_factory=list,
        repr=False,
    )
    delivered_process_receipt_keys: set[tuple[str, str]] = field(
        default_factory=set,
        repr=False,
    )
    operation_process_receipts: dict[str, dict[str, Any] | None] = field(
        default_factory=dict,
        repr=False,
    )
    mutation_fence: _MutationFence | None = field(default=None, repr=False)
    lifecycle_state: str = "open"
    close_task: asyncio.Task[_CloseTransactionResult] | None = field(
        default=None,
        repr=False,
    )
    close_apply_artifacts: bool | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _mutation_fingerprint(
    operation: str,
    payload: dict[str, Any],
) -> str:
    try:
        encoded = json.dumps(
            {
                "operation": operation,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_process_mutation",
            "The persistent process mutation is not canonical JSON.",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


async def _dispatch_mutating_operation(
    record: _ManagedProcess,
    *,
    operation: str,
    operation_id: str,
    fingerprint: str,
    dispatch: Callable[[], Awaitable[dict[str, Any]]],
    cleanup_override: bool = False,
) -> dict[str, Any]:
    """Fence an ambiguous mutation until its exact idempotent replay resolves."""

    prior = record.mutation_fence
    exact_reconcile = bool(
        prior is not None
        and prior.operation == operation
        and prior.operation_id == operation_id
        and prior.fingerprint == fingerprint
    )
    if prior is not None and not exact_reconcile and not cleanup_override:
        raise IsolatedSkillExecutorError(
            "process_mutation_reconcile_required",
            "A prior mutating process dispatch has an unknown outcome. Only "
            "the exact same runtime operation and argument fingerprint may be "
            "replayed; cleanup may still terminate the process.",
        )
    try:
        response = await dispatch()
    except asyncio.CancelledError as exc:
        if getattr(exc, "dispatch_unknown", False):
            record.mutation_fence = _MutationFence(
                operation=operation,
                operation_id=operation_id,
                fingerprint=fingerprint,
            )
        raise
    except IsolatedSkillExecutorError as exc:
        if exc.dispatch_unknown:
            record.mutation_fence = _MutationFence(
                operation=operation,
                operation_id=operation_id,
                fingerprint=fingerprint,
            )
        elif exact_reconcile:
            # A definitive executor response resolves the prior ambiguity,
            # even when the exact idempotent operation itself failed.
            record.mutation_fence = None
        raise
    else:
        if exact_reconcile or prior is None or cleanup_override:
            record.mutation_fence = None
        return response


def _consume_runtime_task_exception(task: asyncio.Task[Any]) -> None:
    """Observe detached runtime-task failures without changing await semantics."""

    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, RuntimeError):
        pass


class SkillProcessManager:
    """In-memory process capabilities; executor leases are never persisted."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[str, _ManagedProcess] = {}
        self._closed: dict[str, tuple[str, str, str]] = {}
        self._closed_evidence: dict[str, dict[str, Any] | None] = {}
        self._pending_starts: dict[str, _PendingProcessStart] = {}
        self._active_cleanups: dict[str, _ActiveProcessCleanup] = {}
        # A generation is scoped to the executor owner and exact execution
        # run.  Cleanup advances it before releasing the manager lock, so a
        # start admitted by an older lifecycle cannot publish after cleanup.
        self._owner_generations: dict[
            tuple[str, str, str, str],
            int,
        ] = {}
        # Reuse one non-model-facing authority capability for a root-run
        # scope. If an open response is lost after both UDS transport attempts,
        # redispatch of the same stable tool call can replay the same
        # (scope_digest, op_id) and recover the executor's cached lease instead
        # of opening an unreachable duplicate.
        self._owner_scopes: dict[
            tuple[str, str, str],
            ProcessOwnerScope,
        ] = {}

    @staticmethod
    def _new_process_id() -> str:
        return "sp_" + secrets.token_urlsafe(32)

    async def start(
        self,
        *,
        context: ToolContext,
        authority: _VerifiedScriptAuthority,
        args: list[str] | None,
        cwd: str,
        idle_ttl_seconds: int,
        max_runtime_seconds: int,
        class_name: str | None,
        factory_name: str | None,
        constructor_args: list[Any] | None,
        constructor_kwargs: dict[str, Any] | None,
        egress_rules: tuple[dict[str, Any], ...],
        private_origins: tuple[str, ...],
        runtime_profile: str,
        socket_path: str,
    ) -> tuple[_ManagedProcess, dict[str, Any]]:
        owner = _owner_key(context)
        egress_budget_binding = (
            session_sandbox_egress_budget_binding(
                context,
                operation="skill_process",
            )
            if egress_rules
            else None
        )
        workspace = sandbox_dir(
            context.user_id,
            context.session_id,
            sub="workspace",
        ).resolve()
        open_op_id = _operation_uuid(context, "open")
        start_op_id = _operation_uuid(context, "start")
        execution_run_id = str(
            context.run_id
            or context.browser_run_scope_id
            or ""
        )
        generation_key = (*owner, execution_run_id)

        # Register a lifecycle-visible pending start while holding the manager
        # lock, but never hold that global lock while waiting for an executor
        # pool slot or doing sidecar I/O.  In particular, a close must be able
        # to release one of the N-1 persistent slots while this start waits.
        async with self._lock:
            if any(
                cleanup.matches(owner, execution_run_id)
                for cleanup in self._active_cleanups.values()
            ):
                raise IsolatedSkillExecutorError(
                    "process_start_cancelled",
                    "The owning runtime scope is being cleaned up; this "
                    "persistent Skill process was not started.",
                )
            owner_scope = self._owner_scopes.get(owner)
            if owner_scope is None:
                owner_scope = create_process_owner_scope(
                    user_id=owner[0],
                    session_id=owner[1],
                    root_run_id=owner[2],
                )
                self._owner_scopes[owner] = owner_scope
            generation = self._owner_generations.setdefault(generation_key, 0)
            pending_id = "sps_" + secrets.token_urlsafe(24)
            while pending_id in self._pending_starts:
                pending_id = "sps_" + secrets.token_urlsafe(24)
            pending = _PendingProcessStart(
                pending_id=pending_id,
                owner=owner,
                execution_run_id=execution_run_id,
                owner_generation=generation,
                owner_scope=owner_scope,
            )
            self._pending_starts[pending_id] = pending

        lease: IsolatedProcessLease | None = None
        published = False

        async def await_start_phase(
            awaitable: Awaitable[Any],
        ) -> tuple[Any, asyncio.CancelledError | None]:
            """Do not let caller cancellation strand a granted reservation."""

            phase_task = asyncio.create_task(awaitable)
            phase_task.add_done_callback(_consume_runtime_task_exception)
            try:
                return await asyncio.shield(phase_task), None
            except asyncio.CancelledError as cancelled:
                # If admission is still queued, the pool atomically removes
                # it. If grant already won, the bounded open/start transaction
                # completes before this start unwinds and rolls back.
                async with self._lock:
                    if self._pending_starts.get(pending_id) is pending:
                        pending.cancel_requested = True
                        pending.admission_cancel_event.set()
                try:
                    result = await asyncio.shield(phase_task)
                except BaseException as phase_error:
                    raise cancelled from phase_error
                return result, cancelled

        try:
            open_result, deferred_cancel = await await_start_phase(
                open_isolated_process_lease(
                    owner_scope=owner_scope,
                    skill_root=authority.skill_root,
                    workspace=workspace,
                    entrypoint=authority.script_resource,
                    args=args,
                    cwd=cwd,
                    idle_ttl_seconds=idle_ttl_seconds,
                    max_runtime_seconds=max_runtime_seconds,
                    class_name=class_name,
                    factory_name=factory_name,
                    constructor_args=constructor_args,
                    constructor_kwargs=constructor_kwargs,
                    **(
                        {
                            "egress_rules": egress_rules,
                            "private_origins": private_origins,
                            "budget_scope_sha256": (
                                egress_budget_binding.budget_scope_sha256
                            ),
                            "call_id_sha256": (
                                egress_budget_binding.call_id_sha256
                            ),
                        }
                        if egress_rules else {}
                    ),
                    socket_path=socket_path,
                    op_id=open_op_id,
                    skill_snapshot=authority.snapshot,
                    admission_cancel_event=(
                        pending.admission_cancel_event
                    ),
                ),
            )
            lease, _opened = open_result
            if deferred_cancel is not None:
                raise deferred_cancel
            async with self._lock:
                cancelled = (
                    self._pending_starts.get(pending_id) is not pending
                    or pending.cancel_requested
                    or self._owner_generations.get(generation_key, 0)
                    != pending.owner_generation
                )
            if cancelled:
                raise IsolatedSkillExecutorError(
                    "process_start_cancelled",
                    "The owning runtime scope was cleaned up before the "
                    "persistent Skill process could start.",
                )
            if (
                lease.script_sha256 != authority.script_sha256
                or lease.skill_sha256 != authority.package_sha256
            ):
                raise IsolatedSkillExecutorError(
                    "skill_script_authority_mismatch",
                    "The executor lease does not match the immutable Skill "
                    "snapshot selected by the compiled authority.",
                )
            started, deferred_cancel = await await_start_phase(
                start_isolated_process_lease(
                    lease,
                    op_id=start_op_id,
                )
            )
            if deferred_cancel is not None:
                raise deferred_cancel

            async with self._lock:
                cancelled = (
                    self._pending_starts.get(pending_id) is not pending
                    or pending.cancel_requested
                    or self._owner_generations.get(generation_key, 0)
                    != pending.owner_generation
                )
                if not cancelled:
                    process_id = self._new_process_id()
                    while (
                        process_id in self._records
                        or process_id in self._closed
                    ):
                        process_id = self._new_process_id()
                    record = _ManagedProcess(
                        process_id=process_id,
                        owner=owner,
                        lease=lease,
                        authority=authority,
                        canonical_script_path=(
                            "skills/"
                            f"{authority.skill_name}/"
                            f"{authority.script_resource}"
                        ),
                        runtime_profile=runtime_profile,
                        execution_run_id=execution_run_id,
                        cli_semantic_task_bound=bool(args),
                    )
                    self._records[process_id] = record
                    self._pending_starts.pop(pending_id, None)
                    pending.done.set()
                    published = True
            if cancelled:
                raise IsolatedSkillExecutorError(
                    "process_start_cancelled",
                    "The owning runtime scope was cleaned up while the "
                    "persistent Skill process was starting.",
                )
            return record, started
        except BaseException as start_exc:
            rollback_failed = False
            if lease is not None and not published:
                try:
                    rollback_task = asyncio.create_task(
                        close_isolated_process_lease(
                            lease,
                            op_id=_operation_uuid(
                                context,
                                "rollback-close",
                            ),
                            apply_artifacts=False,
                        )
                    )
                    rollback_task.add_done_callback(
                        _consume_runtime_task_exception
                    )
                    await asyncio.shield(rollback_task)
                except BaseException:
                    rollback_failed = True
            async with self._lock:
                if rollback_failed and lease is not None:
                    # The executor may already own a live/open lease even
                    # though start or the post-snapshot authority check
                    # failed. Keep an internal, unreturned record so the
                    # cleanup which cancelled this pending start (or a later
                    # root/session/shutdown cleanup) can retry exact close.
                    process_id = self._new_process_id()
                    while (
                        process_id in self._records
                        or process_id in self._closed
                    ):
                        process_id = self._new_process_id()
                    self._records[process_id] = _ManagedProcess(
                        process_id=process_id,
                        owner=owner,
                        lease=lease,
                        authority=authority,
                        canonical_script_path=(
                            "skills/"
                            f"{authority.skill_name}/"
                            f"{authority.script_resource}"
                        ),
                        runtime_profile=runtime_profile,
                        execution_run_id=execution_run_id,
                        cli_semantic_task_bound=bool(args),
                    )
                if self._pending_starts.get(pending_id) is pending:
                    self._pending_starts.pop(pending_id, None)
                pending.done.set()
                if (
                    pending.cancel_requested
                    and not any(
                        record.owner == owner
                        for record in self._records.values()
                    )
                    and not any(
                        other.owner == owner
                        for other in self._pending_starts.values()
                    )
                ):
                    self._owner_scopes.pop(owner, None)
                    for owned_generation_key in list(
                        self._owner_generations
                    ):
                        if owned_generation_key[:3] == owner:
                            self._owner_generations.pop(
                                owned_generation_key,
                                None,
                            )
            if (
                isinstance(start_exc, IsolatedSkillExecutorError)
                and start_exc.code == "executor_admission_cancelled"
                and pending.cancel_requested
            ):
                raise IsolatedSkillExecutorError(
                    "process_start_cancelled",
                    "The owning runtime cleanup cancelled this process start "
                    "while it was still waiting for executor admission.",
                ) from start_exc
            raise

    async def acquire(
        self,
        process_id: str,
        context: ToolContext,
    ) -> _ManagedProcess:
        owner = _owner_key(context)
        async with self._lock:
            record = self._records.get(process_id)
            if record is None:
                closed_owner = self._closed.get(process_id)
                if closed_owner == owner:
                    raise IsolatedSkillExecutorError(
                        "process_closed",
                        "The persistent Skill process is already closed.",
                    )
                if closed_owner is not None:
                    raise IsolatedSkillExecutorError(
                        "process_scope_mismatch",
                        "The process capability belongs to a different runtime scope.",
                    )
                raise IsolatedSkillExecutorError(
                    "lease_lost",
                    "The runtime no longer has this process capability. It may "
                    "have expired or the Harness may have restarted; start a "
                    "new exact Skill process.",
                )
            if record.owner != owner:
                raise IsolatedSkillExecutorError(
                    "process_scope_mismatch",
                    "The process capability belongs to a different session or root run.",
                )
            if record.lifecycle_state == "closing":
                raise IsolatedSkillExecutorError(
                    "process_closing",
                    "The persistent Skill process is being closed by its "
                    "owning runtime.",
                )
            if record.lifecycle_state != "open":
                raise IsolatedSkillExecutorError(
                    "lease_lost",
                    "The persistent Skill process is no longer open.",
                )
            if not _context_permits_record(context, record.authority):
                raise IsolatedSkillExecutorError(
                    "process_authority_mismatch",
                    "The current compiled task does not carry the exact authority "
                    "that created this process.",
                )
            return record

    async def forget_lost(
        self,
        record: _ManagedProcess,
        error: IsolatedSkillExecutorError,
    ) -> bool:
        """Finalize the physical reservation before forgetting a terminal lease."""

        if not await finalize_terminal_process_lease_error(
            record.lease,
            error,
        ):
            return False
        async with self._lock:
            if self._records.get(record.process_id) is record:
                self._records.pop(record.process_id, None)
                record.lifecycle_state = "lost"
        return True

    def _cache_closed_locked(
        self,
        record: _ManagedProcess,
        evidence_receipt: dict[str, Any] | None,
    ) -> None:
        self._records.pop(record.process_id, None)
        record.lifecycle_state = "closed"
        self._closed[record.process_id] = record.owner
        self._closed_evidence[record.process_id] = (
            dict(evidence_receipt)
            if evidence_receipt is not None
            else None
        )
        while len(self._closed) > 2_048:
            stale = next(iter(self._closed))
            self._closed.pop(stale, None)
            self._closed_evidence.pop(stale, None)

    async def _run_close_transaction(
        self,
        record: _ManagedProcess,
        *,
        operation_id: str,
        fingerprint: str,
        apply_artifacts: bool,
        discard_pending_artifacts: bool,
        execution_authority_check: Callable[[], None] | None,
        cleanup_override: bool,
    ) -> _CloseTransactionResult:
        try:
            async with record.lock:
                response = await _dispatch_mutating_operation(
                    record,
                    operation="close",
                    operation_id=operation_id,
                    fingerprint=fingerprint,
                    cleanup_override=cleanup_override,
                    dispatch=lambda: close_isolated_process_lease(
                        record.lease,
                        op_id=operation_id,
                        execution_authority_check=(
                            execution_authority_check
                        ),
                        apply_artifacts=apply_artifacts,
                        discard_pending_artifacts=(
                            discard_pending_artifacts
                        ),
                    ),
                )
                evidence_receipt = _terminal_process_evidence_receipt(
                    record,
                    "close",
                    response,
                    operation_id=operation_id,
                )
        except BaseException as exc:
            terminal = False
            if isinstance(exc, IsolatedSkillExecutorError):
                try:
                    terminal = await finalize_terminal_process_lease_error(
                        record.lease,
                        exc,
                    )
                except BaseException:
                    async with self._lock:
                        if self._records.get(record.process_id) is record:
                            record.lifecycle_state = "open"
                            record.close_task = None
                    raise
            async with self._lock:
                if self._records.get(record.process_id) is record:
                    if terminal:
                        self._records.pop(record.process_id, None)
                        record.lifecycle_state = "lost"
                    else:
                        # A definitive/transient failure returns the record to
                        # OPEN atomically. An ambiguous close retains its
                        # mutation fence, so only exact reconcile or cleanup
                        # may dispatch another mutation.
                        record.lifecycle_state = "open"
                        record.close_task = None
            raise

        async with self._lock:
            if self._records.get(record.process_id) is record:
                self._cache_closed_locked(record, evidence_receipt)
        return _CloseTransactionResult(
            response=dict(response),
            evidence_receipt=(
                dict(evidence_receipt)
                if evidence_receipt is not None
                else None
            ),
        )

    def _ensure_close_task_locked(
        self,
        record: _ManagedProcess,
        *,
        operation_id: str,
        apply_artifacts: bool,
        discard_pending_artifacts: bool = False,
        execution_authority_check: Callable[[], None] | None,
        cleanup_override: bool,
    ) -> asyncio.Task[_CloseTransactionResult]:
        if (
            record.lifecycle_state == "closing"
            and record.close_task is not None
        ):
            if record.close_apply_artifacts is not bool(apply_artifacts):
                raise IsolatedSkillExecutorError(
                    "pending_sync_intent_mismatch",
                    "The active close transaction is bound to a different "
                    "apply_artifacts intent.",
                )
            return record.close_task
        if record.lifecycle_state != "open":
            raise IsolatedSkillExecutorError(
                "lease_lost",
                "The persistent Skill process is no longer open.",
            )
        if (
            record.close_apply_artifacts is not None
            and record.close_apply_artifacts is not bool(apply_artifacts)
        ):
            raise IsolatedSkillExecutorError(
                "pending_sync_intent_mismatch",
                "The retained close transaction is bound to a different "
                "apply_artifacts intent.",
            )
        fingerprint = _mutation_fingerprint(
            "close",
            {"apply_artifacts": bool(apply_artifacts)},
        )
        record.close_apply_artifacts = bool(apply_artifacts)
        record.lifecycle_state = "closing"
        record.close_task = asyncio.create_task(
            self._run_close_transaction(
                record,
                operation_id=operation_id,
                fingerprint=fingerprint,
                apply_artifacts=apply_artifacts,
                discard_pending_artifacts=discard_pending_artifacts,
                execution_authority_check=execution_authority_check,
                cleanup_override=cleanup_override,
            ),
            name=f"skill-process-close:{record.process_id}",
        )
        record.close_task.add_done_callback(
            _consume_runtime_task_exception
        )
        return record.close_task

    async def _close_session_deletion_record(
        self,
        record: _ManagedProcess,
    ) -> _CloseTransactionResult:
        """Join an earlier close, then discard any still-unapplied batch.

        A root-run ``finally`` may already have started an artifact-applying
        close before session cleanup observes the record.  The durable session
        tombstone makes that apply fail safely.  This helper joins that exact
        transaction first and, on a non-terminal failure, retries through the
        trusted discard+ACK path instead of retaining an impossible apply
        intent forever.
        """

        prior_task: asyncio.Task[_CloseTransactionResult] | None = None
        discard_task: asyncio.Task[_CloseTransactionResult] | None = None
        async with self._lock:
            if self._records.get(record.process_id) is not record:
                return _CloseTransactionResult(
                    response={
                        "status": "success",
                        "state": "closed",
                        "workspace_applied": False,
                        "artifacts": [],
                    },
                    evidence_receipt=None,
                )
            if (
                record.lifecycle_state == "closing"
                and record.close_task is not None
            ):
                prior_task = record.close_task
            elif record.lifecycle_state == "open":
                # Only session deletion may supersede a retained apply intent.
                # The isolated lease still proves and ACKs the exact prepared
                # batch; this reset merely lets the manager request discard.
                record.close_apply_artifacts = None
                discard_task = self._ensure_close_task_locked(
                    record,
                    operation_id=str(uuid.uuid4()),
                    apply_artifacts=False,
                    discard_pending_artifacts=True,
                    execution_authority_check=None,
                    cleanup_override=True,
                )
            else:
                raise IsolatedSkillExecutorError(
                    "lease_lost",
                    "The persistent Skill process is no longer open.",
                )

        if prior_task is not None:
            try:
                return await asyncio.shield(prior_task)
            except BaseException:
                current = asyncio.current_task()
                if (
                    current is not None
                    and current.cancelling()
                ):
                    raise
                async with self._lock:
                    if self._records.get(record.process_id) is not record:
                        # A terminal close error already removed the record;
                        # preserve it so the caller can classify the physical
                        # lease outcome instead of reporting false success.
                        raise
                    if record.lifecycle_state != "open":
                        raise
                    record.close_apply_artifacts = None
                    discard_task = self._ensure_close_task_locked(
                        record,
                        operation_id=str(uuid.uuid4()),
                        apply_artifacts=False,
                        discard_pending_artifacts=True,
                        execution_authority_check=None,
                        cleanup_override=True,
                    )

        if discard_task is None:
            raise IsolatedSkillExecutorError(
                "invalid_lease_state",
                "Session cleanup could not establish a close transaction.",
            )
        return await asyncio.shield(discard_task)

    async def close_explicit(
        self,
        process_id: str,
        context: ToolContext,
    ) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
        owner = _owner_key(context)
        async with self._lock:
            record = self._records.get(process_id)
            if record is None:
                closed_owner = self._closed.get(process_id)
                if closed_owner == owner:
                    return {
                        "status": "success",
                        "state": "closed",
                    }, True, (
                        dict(self._closed_evidence[process_id])
                        if isinstance(
                            self._closed_evidence.get(process_id),
                            dict,
                        )
                        else None
                    )
                if closed_owner is not None:
                    raise IsolatedSkillExecutorError(
                        "process_scope_mismatch",
                        "The process capability belongs to a different runtime scope.",
                    )
                raise IsolatedSkillExecutorError(
                    "lease_lost",
                    "The runtime no longer has this process capability.",
                )
            if record.owner != owner:
                raise IsolatedSkillExecutorError(
                    "process_scope_mismatch",
                    "The process capability belongs to a different session or root run.",
                )
            if not _context_permits_record(context, record.authority):
                raise IsolatedSkillExecutorError(
                    "process_authority_mismatch",
                    "The current compiled task does not carry this process authority.",
                )
            close_task = self._ensure_close_task_locked(
                record,
                operation_id=_operation_uuid(context, "close"),
                apply_artifacts=True,
                discard_pending_artifacts=False,
                execution_authority_check=lambda: (
                    require_execution_authority(
                        context,
                        boundary="skill_process.close.commit",
                    )
                ),
                cleanup_override=False,
            )
        # The close transaction belongs to the runtime record. Cancelling an
        # HTTP/SSE caller only cancels its wait; explicit close, cleanup, or a
        # retry can join the same still-running transaction.
        result = await asyncio.shield(close_task)
        return (
            dict(result.response),
            False,
            (
                dict(result.evidence_receipt)
                if result.evidence_receipt is not None
                else None
            ),
        )

    async def cleanup_root(
        self,
        user_id: str,
        session_id: str,
        root_run_id: str,
    ) -> dict[str, Any]:
        return await self._cleanup_matching(
            lambda owner: owner == (user_id, session_id, root_run_id),
            reason="root_run",
        )

    async def cleanup_run(
        self,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Close leases created by one exact child/root run."""

        normalized_run = str(run_id or "")
        return await self._cleanup_matching(
            lambda owner, record=None: False,
            reason="run",
            execution_run=(
                str(user_id),
                str(session_id),
                normalized_run,
            ),
        )

    async def cleanup_session(
        self,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        result = await self._cleanup_matching(
            lambda owner: owner[:2] == (user_id, session_id),
            reason="session",
        )
        async with self._lock:
            for process_id, owner in list(self._closed.items()):
                if owner[:2] == (user_id, session_id):
                    self._closed.pop(process_id, None)
                    self._closed_evidence.pop(process_id, None)
        return result

    async def close_all(self) -> dict[str, Any]:
        return await self._cleanup_matching(lambda _owner: True, reason="shutdown")

    async def _cleanup_matching(
        self,
        predicate: Any,
        *,
        reason: str,
        execution_run: tuple[str, str, str] | None = None,
    ) -> dict[str, Any]:
        cleanup = _ActiveProcessCleanup(
            cleanup_id="spc_" + secrets.token_urlsafe(24),
            predicate=predicate,
            execution_run=execution_run,
        )
        attempted_process_ids: set[str] = set()
        seen_pending_ids: set[str] = set()
        timed_out_pending_ids: set[str] = set()
        bumped_generation_keys: set[tuple[str, str, str, str]] = set()
        outcomes: list[tuple[_ManagedProcess, str | None, bool]] = []

        async def close_one(
            record: _ManagedProcess,
            close_task: asyncio.Task[_CloseTransactionResult],
        ) -> tuple[_ManagedProcess, str | None, bool]:
            try:
                await asyncio.shield(close_task)
                return record, None, False
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                code = (
                    exc.code
                    if isinstance(exc, IsolatedSkillExecutorError)
                    else type(exc).__name__
                )
                terminal = (
                    isinstance(exc, IsolatedSkillExecutorError)
                    and terminal_process_lease_error_action(exc) is not None
                )
                return record, str(code), terminal

        async with self._lock:
            self._active_cleanups[cleanup.cleanup_id] = cleanup
        try:
            while True:
                async with self._lock:
                    # Advance every matching known start lifecycle before
                    # releasing the lock. Pending starts also carry an
                    # explicit cancellation bit so the check is fail closed
                    # even if generation bookkeeping is later compacted.
                    for generation_key, generation in list(
                        self._owner_generations.items()
                    ):
                        if (
                            generation_key not in bumped_generation_keys
                            and cleanup.matches(
                                generation_key[:3],
                                generation_key[3],
                            )
                        ):
                            self._owner_generations[generation_key] = (
                                generation + 1
                            )
                            bumped_generation_keys.add(generation_key)

                    pending_starts = [
                        pending
                        for pending in self._pending_starts.values()
                        if (
                            cleanup.matches(
                                pending.owner,
                                pending.execution_run_id,
                            )
                            and pending.pending_id
                            not in timed_out_pending_ids
                        )
                    ]
                    for pending in pending_starts:
                        pending.cancel_requested = True
                        pending.admission_cancel_event.set()
                        seen_pending_ids.add(pending.pending_id)

                    records = [
                        record
                        for record in self._records.values()
                        if (
                            cleanup.matches(
                                record.owner,
                                record.execution_run_id,
                            )
                            and record.process_id
                            not in attempted_process_ids
                        )
                    ]
                    close_tasks: list[
                        tuple[
                            _ManagedProcess,
                            asyncio.Task[_CloseTransactionResult],
                        ]
                    ] = []
                    for record in records:
                        attempted_process_ids.add(record.process_id)
                        if reason == "session":
                            close_task = asyncio.create_task(
                                self._close_session_deletion_record(record),
                                name=(
                                    "skill-process-session-delete:"
                                    f"{record.process_id}"
                                ),
                            )
                        else:
                            try:
                                close_task = self._ensure_close_task_locked(
                                    record,
                                    operation_id=str(uuid.uuid4()),
                                    apply_artifacts=reason != "run",
                                    discard_pending_artifacts=False,
                                    execution_authority_check=None,
                                    cleanup_override=True,
                                )
                            except IsolatedSkillExecutorError as exc:
                                outcomes.append((
                                    record,
                                    exc.code,
                                    (
                                        terminal_process_lease_error_action(exc)
                                        is not None
                                    ),
                                ))
                                continue
                        close_tasks.append((record, close_task))

                # Close already-published records first. This releases a pool
                # slot which a matching pending start may be waiting for. The
                # pending start then observes cancellation, rolls back its
                # newly opened lease, and signals ``done``.
                close_waiters = {
                    asyncio.create_task(close_one(record, close_task)): record
                    for record, close_task in close_tasks
                }
                batch: list[
                    tuple[_ManagedProcess, str | None, bool]
                ] = []
                if close_waiters:
                    done, waiting = await asyncio.wait(
                        close_waiters,
                        timeout=MAX_PROCESS_CLOSE_CLEANUP_WAIT_SECONDS,
                    )
                    for waiter in done:
                        batch.append(waiter.result())
                    for waiter in waiting:
                        record = close_waiters[waiter]
                        # Cancelling this waiter cannot cancel the shielded,
                        # runtime-owned close transaction.
                        waiter.cancel()
                        batch.append((
                            record,
                            "process_close_cleanup_timeout",
                            False,
                        ))
                    if waiting:
                        await asyncio.gather(
                            *waiting,
                            return_exceptions=True,
                        )
                outcomes.extend(batch)

                if pending_starts:
                    pending_waiters = {
                        asyncio.create_task(pending.done.wait()): pending
                        for pending in pending_starts
                    }
                    done, waiting = await asyncio.wait(
                        pending_waiters,
                        timeout=MAX_PENDING_START_CLEANUP_WAIT_SECONDS,
                    )
                    del done
                    for waiter in waiting:
                        pending = pending_waiters[waiter]
                        timed_out_pending_ids.add(pending.pending_id)
                        waiter.cancel()
                    if waiting:
                        await asyncio.gather(
                            *waiting,
                            return_exceptions=True,
                        )

                # A cancelled start whose immediate rollback failed publishes
                # one hidden record just before signalling done. Iterate once
                # more so this same cleanup can close it rather than orphan it.
                async with self._lock:
                    has_pending = any(
                        cleanup.matches(
                            pending.owner,
                            pending.execution_run_id,
                        )
                        and pending.pending_id
                        not in timed_out_pending_ids
                        for pending in self._pending_starts.values()
                    )
                    has_unattempted_record = any(
                        cleanup.matches(
                            record.owner,
                            record.execution_run_id,
                        )
                        and record.process_id not in attempted_process_ids
                        for record in self._records.values()
                    )
                if not has_pending and not has_unattempted_record:
                    break
        finally:
            async with self._lock:
                self._active_cleanups.pop(cleanup.cleanup_id, None)
                failed_owners = {
                    record.owner
                    for record, error_code, terminal in outcomes
                    if error_code is not None and not terminal
                }
                for owner in list(self._owner_scopes):
                    if (
                        (
                            cleanup.matches(owner, "")
                            if execution_run is None
                            else owner[:2] == execution_run[:2]
                        )
                        and owner not in failed_owners
                        and not any(
                            record.owner == owner
                            for record in self._records.values()
                        )
                        and not any(
                            pending.owner == owner
                            for pending in self._pending_starts.values()
                        )
                    ):
                        self._owner_scopes.pop(owner, None)
                        for generation_key in list(
                            self._owner_generations
                        ):
                            if generation_key[:3] == owner:
                                self._owner_generations.pop(
                                    generation_key,
                                    None,
                                )
        failures = [
            {
                "process_id": record.process_id,
                "error_code": error_code,
                "retained_for_retry": not terminal,
            }
            for record, error_code, terminal in outcomes
            if error_code is not None and not terminal
        ]
        pending_failures = [
            {
                "pending_start_id": pending_id,
                "error_code": "process_start_cleanup_timeout",
                "retained_for_retry": True,
            }
            for pending_id in sorted(timed_out_pending_ids)
        ]
        failures.extend(pending_failures)
        terminal_lost = sum(
            1
            for _record, error_code, terminal in outcomes
            if error_code is not None and terminal
        )
        return {
            "success": not failures,
            "reason": reason,
            "matched": len(outcomes),
            "closed": sum(
                1
                for _record, error_code, terminal in outcomes
                if error_code is None or terminal
            ),
            "terminal_lost": terminal_lost,
            "retained_for_retry": len(failures),
            "pending_starts_cancelled": len(seen_pending_ids),
            "pending_starts_timed_out": len(timed_out_pending_ids),
            "failures": failures,
        }


_MANAGER = SkillProcessManager()


def _validate_process_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_PROCESS_ID_CHARS
        or _PROCESS_ID_RE.fullmatch(value) is None
    ):
        raise IsolatedSkillExecutorError(
            "invalid_process_id",
            "process_id must be the opaque value returned by start.",
        )
    return value


def _decode_stdin(data: str | None, data_base64: str | None) -> bytes | str:
    if (data is None) == (data_base64 is None):
        raise IsolatedSkillExecutorError(
            "invalid_stdin",
            "write requires exactly one of data or data_base64.",
        )
    if data is not None:
        if not isinstance(data, str):
            raise IsolatedSkillExecutorError(
                "invalid_stdin",
                "data must be UTF-8 text.",
            )
        try:
            encoded = data.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise IsolatedSkillExecutorError(
                "invalid_stdin",
                "data must be valid UTF-8 text.",
            ) from exc
        if not encoded or len(encoded) > MAX_PROCESS_STDIN_CHUNK_BYTES:
            raise IsolatedSkillExecutorError(
                "invalid_stdin",
                f"write data must contain 1 to {MAX_PROCESS_STDIN_CHUNK_BYTES} bytes.",
            )
        return data
    try:
        decoded = base64.b64decode(
            str(data_base64).encode("ascii"),
            validate=True,
        )
    except (UnicodeError, binascii.Error, ValueError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_stdin",
            "data_base64 must be canonical bounded base64.",
        ) from exc
    if not decoded or len(decoded) > MAX_PROCESS_STDIN_CHUNK_BYTES:
        raise IsolatedSkillExecutorError(
            "invalid_stdin",
            f"decoded stdin must contain 1 to {MAX_PROCESS_STDIN_CHUNK_BYTES} bytes.",
        )
    return decoded


def _process_receipt_common(record: _ManagedProcess) -> dict[str, str]:
    return {
        "skill_name": record.authority.skill_name,
        "script_resource": record.authority.script_resource,
        "script_sha256": record.authority.script_sha256,
        "package_sha256": record.authority.package_sha256,
        "process_id": record.process_id,
        "invocation_mode": record.lease.invocation_mode,
    }


def _observe_cli_stdout(
    record: _ManagedProcess,
    response: dict[str, Any],
) -> None:
    """Hash one complete contiguous CLI stdout stream without retaining it."""

    if (
        record.lease.invocation_mode != "cli"
        or record.cli_stdout_observation_failed
    ):
        return
    raw = response.get("stdout_bytes")
    start = response.get("stdout_start_offset")
    next_offset = response.get("stdout_next_offset")
    if (
        not isinstance(raw, bytes)
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(next_offset, bool)
        or not isinstance(next_offset, int)
        or start < 0
        or next_offset < start
        or next_offset - start != len(raw)
    ):
        record.cli_stdout_observation_failed = True
        return
    if (
        response.get("stdout_data_loss") is True
        and start > record.cli_stdout_observed_offset
    ):
        record.cli_stdout_observation_failed = True
        return
    if next_offset <= record.cli_stdout_observed_offset:
        return
    if start > record.cli_stdout_observed_offset:
        record.cli_stdout_observation_failed = True
        return
    overlap = record.cli_stdout_observed_offset - start
    if overlap < 0 or overlap > len(raw):
        record.cli_stdout_observation_failed = True
        return
    new_bytes = raw[overlap:]
    record.cli_stdout_hasher.update(new_bytes)
    record.cli_stdout_size_bytes += len(new_bytes)
    record.cli_stdout_observed_offset = next_offset


def _queue_structured_call_results(
    record: _ManagedProcess,
    response: dict[str, Any],
) -> None:
    """Parse only a contiguous executor-authenticated stdout JSONL prefix."""

    if (
        record.lease.invocation_mode not in {"instance", "factory"}
        or record.stdout_receipt_parsing_failed
    ):
        return
    raw = response.get("stdout_bytes")
    start = response.get("stdout_start_offset")
    next_offset = response.get("stdout_next_offset")
    if (
        not isinstance(raw, bytes)
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(next_offset, bool)
        or not isinstance(next_offset, int)
        or start < 0
        or next_offset < start
        or next_offset - start != len(raw)
    ):
        record.stdout_receipt_parsing_failed = True
        return
    if response.get("stdout_data_loss") is True and start > record.stdout_parse_offset:
        record.stdout_receipt_parsing_failed = True
        return
    if next_offset <= record.stdout_parse_offset:
        return
    if start > record.stdout_parse_offset:
        # The caller may still retrieve the missing interval. Do not consume a
        # later chunk or manufacture a cross-gap JSON line.
        return
    overlap = record.stdout_parse_offset - start
    if overlap < 0 or overlap > len(raw):
        return
    record.stdout_line_fragment.extend(raw[overlap:])
    record.stdout_parse_offset = next_offset
    if len(record.stdout_line_fragment) > MAX_PROCESS_RECEIPT_LINE_BYTES:
        record.stdout_line_fragment.clear()
        record.stdout_receipt_parsing_failed = True
        return

    while b"\n" in record.stdout_line_fragment:
        raw_line, remainder = bytes(record.stdout_line_fragment).split(b"\n", 1)
        record.stdout_line_fragment[:] = remainder
        if not raw_line or len(raw_line) > MAX_PROCESS_RECEIPT_LINE_BYTES:
            continue
        try:
            envelope = json.loads(raw_line.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(envelope, dict)
            or envelope.get("event") != "call_result"
            or envelope.get("status") not in {"success", "error"}
        ):
            continue
        call_id = envelope.get("call_id")
        pending = (
            record.pending_structured_calls.get(call_id)
            if isinstance(call_id, str)
            else None
        )
        if (
            pending is None
            or envelope.get("method") != pending.method_name
        ):
            continue
        record.pending_structured_calls.pop(pending.call_id, None)
        if not pending.semantic_task_bound:
            continue
        callable_receipt = build_callable_skill_result_receipt(
            "run_skill_process",
            (
                {"result": envelope.get("result")}
                if envelope["status"] == "success"
                else {}
            ),
        )
        if callable_receipt is None:  # pragma: no cover - fixed runner kind
            continue
        outcome = (
            "error"
            if (
                envelope["status"] == "error"
                or callable_skill_result_receipt_is_failure(
                    callable_receipt
                )
            )
            else "success"
        )
        try:
            receipt = build_skill_process_evidence_receipt(
                **_process_receipt_common(record),
                completion_kind="structured_call",
                outcome=outcome,
                call_id=pending.call_id,
                method_name=pending.method_name,
                call_result_status=envelope["status"],
                callable_result_receipt=callable_receipt,
            )
        except ValueError:
            continue
        if not any(
            item.get("receipt_id") == receipt["receipt_id"]
            for item in record.completed_process_receipts
        ):
            record.completed_process_receipts.append(receipt)


def _artifact_process_receipt(
    record: _ManagedProcess,
    operation: str,
    response: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        operation not in {"sync", "close"}
        or record.lease.invocation_mode not in {"instance", "factory"}
        or response.get("workspace_applied") is not True
        or response.get("sync_pending") is not False
        or response.get("sync_acknowledged") is not True
        or response.get("acknowledged_operation") != operation
        or (
            operation == "close"
            and (
                isinstance(response.get("returncode"), bool)
                or not isinstance(response.get("returncode"), int)
                or response["returncode"] != 0
            )
        )
    ):
        return None
    call_id = record.latest_semantic_call_id
    call = record.structured_call_history.get(str(call_id or ""))
    terminal_success = next(
        (
            receipt
            for receipt in record.completed_process_receipts
            if (
                receipt.get("completion_kind") == "structured_call"
                and receipt.get("call_id") == call_id
                and receipt.get("outcome") == "success"
            )
        ),
        None,
    )
    if (
        call is None
        or not call.semantic_task_bound
        or terminal_success is None
    ):
        return None
    artifacts = response.get("artifacts")
    manifest = (
        skill_process_artifact_manifest_sha256(artifacts)
        if isinstance(artifacts, list)
        else None
    )
    if manifest is None:
        return None
    try:
        return build_skill_process_evidence_receipt(
            **_process_receipt_common(record),
            completion_kind=(
                "artifact_sync" if operation == "sync" else "artifact_close"
            ),
            outcome="success",
            artifact_count=manifest[0],
            artifact_manifest_sha256=manifest[1],
            call_id=call.call_id,
            method_name=call.method_name,
        )
    except ValueError:
        return None


def _terminal_process_evidence_receipt(
    record: _ManagedProcess,
    operation: str,
    response: dict[str, Any],
    *,
    operation_id: str,
) -> dict[str, Any] | None:
    cached = record.operation_process_receipts.get(operation_id, ...)
    if cached is not ...:
        return dict(cached) if isinstance(cached, dict) else None

    receipt: dict[str, Any] | None = None
    if operation == "read":
        _queue_structured_call_results(record, response)
        _observe_cli_stdout(record, response)
        stdout_end_offset = response.get("stdout_end_offset")
        complete_nonempty_cli_stdout = bool(
            record.lease.invocation_mode == "cli"
            and not record.cli_stdout_observation_failed
            and record.cli_stdout_size_bytes > 0
            and isinstance(stdout_end_offset, int)
            and not isinstance(stdout_end_offset, bool)
            and record.cli_stdout_observed_offset == stdout_end_offset
        )
        cli_task_bound = bool(
            record.cli_semantic_task_bound
            or complete_nonempty_cli_stdout
        )
        if (
            record.lease.invocation_mode == "cli"
            and cli_task_bound
            and response.get("stdout_eof") is True
            and response.get("stderr_eof") is True
            and response.get("state") in {"exited", "closed"}
            and isinstance(response.get("returncode"), int)
            and not isinstance(response.get("returncode"), bool)
        ):
            try:
                cli_receipt = build_skill_process_evidence_receipt(
                    **_process_receipt_common(record),
                    completion_kind="cli_exit",
                    outcome=(
                        "success"
                        if response["returncode"] == 0
                        else "error"
                    ),
                    returncode=response["returncode"],
                    **(
                        {
                            "stdout_size_bytes": (
                                record.cli_stdout_size_bytes
                            ),
                            "stdout_sha256": (
                                record.cli_stdout_hasher.hexdigest()
                            ),
                        }
                        if complete_nonempty_cli_stdout
                        else {}
                    ),
                )
            except ValueError:
                cli_receipt = None
            if (
                cli_receipt is not None
                and not any(
                    item.get("receipt_id") == cli_receipt["receipt_id"]
                    for item in record.completed_process_receipts
                )
            ):
                record.completed_process_receipts.append(cli_receipt)
        receipt = next(
            (
                item
                for item in record.completed_process_receipts
                if (
                    item["receipt_id"],
                    item["completion_kind"],
                )
                not in record.delivered_process_receipt_keys
            ),
            None,
        )
    elif operation in {"sync", "close"}:
        receipt = _artifact_process_receipt(record, operation, response)

    if receipt is not None:
        receipt_key = (
            str(receipt["receipt_id"]),
            str(receipt["completion_kind"]),
        )
        if receipt_key in record.delivered_process_receipt_keys:
            receipt = None
        else:
            record.delivered_process_receipt_keys.add(receipt_key)
    if len(record.operation_process_receipts) >= 1_024:
        record.operation_process_receipts.pop(
            next(iter(record.operation_process_receipts)),
            None,
        )
    record.operation_process_receipts[operation_id] = (
        dict(receipt) if receipt is not None else None
    )
    return dict(receipt) if receipt is not None else None


def _public_receipt(
    operation: str,
    process_id: str,
    response: dict[str, Any],
    *,
    already_closed: bool = False,
    process_evidence_receipt: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "status": "success",
        "operation": operation,
        "process_id": process_id,
    }
    for field_name in (
        "state",
        "runtime_profile",
        "network_policy",
        "interpreter",
        "invocation_mode",
        "class_name",
        "factory_name",
        "method_name",
        "call_id",
        "bytes_written",
        "requested_bytes",
        "partial",
        "stdin_total_bytes",
        "stdin_closed",
        "already_closed",
        "signal",
        "signal_delivered",
        "returncode",
        "deleted_workspace_files_ignored",
        "workspace_applied",
        "sync_pending",
        "sync_acknowledged",
        "acknowledged_operation",
    ):
        if field_name in response:
            payload[field_name] = response[field_name]
    if already_closed:
        payload["already_closed"] = True
    if process_evidence_receipt is not None:
        payload["process_evidence_receipt"] = dict(
            process_evidence_receipt
        )
    if operation == "call":
        payload["call_enqueued"] = not bool(response.get("partial"))
        payload["result_delivery"] = "stdout_jsonl"
        payload["next_action"] = (
            "Call is only enqueued. Use read from the prior retained offsets "
            "(0 initially), then advance with each read receipt's "
            "stdout_next_offset/stderr_next_offset until stdout contains the "
            "matching call_result/call_id. Then use sync before inspecting any "
            "workspace artifact created by the method."
        )
    artifacts = response.get("artifacts")
    if isinstance(artifacts, list):
        payload["artifacts"] = [
            dict(item)
            for item in artifacts[:512]
            if isinstance(item, dict)
        ]
    if operation == "read":
        for stream_name in ("stdout", "stderr"):
            raw = response.get(f"{stream_name}_bytes")
            if not isinstance(raw, bytes):
                raw = b""
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeError:
                text = None
            if text is not None:
                payload[f"{stream_name}_text"] = text[:MAX_PUBLIC_RESULT_TEXT_CHARS]
                payload[f"{stream_name}_encoding"] = "utf-8"
            else:
                payload[f"{stream_name}_base64"] = base64.b64encode(raw).decode(
                    "ascii"
                )
                payload[f"{stream_name}_encoding"] = "base64"
            for suffix in (
                "start_offset",
                "next_offset",
                "end_offset",
                "data_loss",
                "truncated",
                "eof",
            ):
                key = f"{stream_name}_{suffix}"
                if key in response:
                    payload[key] = response[key]
    return json.dumps(payload, ensure_ascii=False)


async def run_skill_process(
    operation: str,
    script_path: str | None = None,
    process_id: str | None = None,
    args: list[str] | None = None,
    cwd: str = "workspace",
    idle_ttl_seconds: int = DEFAULT_IDLE_TTL_SECONDS,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    class_name: str | None = None,
    factory_name: str | None = None,
    constructor_args: list[Any] | None = None,
    constructor_kwargs: dict[str, Any] | None = None,
    data: str | None = None,
    data_base64: str | None = None,
    method_name: str | None = None,
    method_args: list[Any] | None = None,
    method_kwargs: dict[str, Any] | None = None,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
    max_bytes: int = MAX_PROCESS_READ_BYTES,
    wait_ms: int = 0,
    signal: str | None = None,
    context: ToolContext | None = None,
) -> str:
    """Operate one runtime-owned persistent process for an exact Skill."""

    if context is None:
        return _json_error(
            "missing_runtime_context",
            "Persistent Skill execution requires runtime-owned ToolContext.",
        )
    active_record: _ManagedProcess | None = None
    try:
        require_execution_authority(
            context,
            boundary=f"skill_process.{operation}.entry",
        )
        if operation not in {
            "start", "write", "stdin_close", "read", "call", "sync",
            "signal", "close",
        }:
            raise IsolatedSkillExecutorError(
                "invalid_process_operation",
                "operation must be start, write, stdin_close, read, call, "
                "sync, signal, or close.",
            )
        if operation == "start":
            if process_id is not None:
                raise IsolatedSkillExecutorError(
                    "invalid_process_operation",
                    "start accepts script_path, not process_id.",
                )
            authority = _verify_exact_script_authority(
                str(script_path or ""),
                context,
            )
            selected_class = _safe_public_identifier(
                class_name,
                field_name="class_name",
            )
            selected_factory = _safe_public_identifier(
                factory_name,
                field_name="factory_name",
            )
            if selected_class is not None and selected_factory is not None:
                raise IsolatedSkillExecutorError(
                    "invalid_process_invocation",
                    "class_name and factory_name are mutually exclusive.",
                )
            if selected_class is not None or selected_factory is not None:
                if authority.script.suffix.casefold() != ".py":
                    raise IsolatedSkillExecutorError(
                        "invalid_process_invocation",
                        "Structured class/factory sessions require a Python entrypoint.",
                    )
                source = authority.snapshot.read_bytes(
                    authority.script_resource
                ).decode("utf-8", errors="replace")
                declared_classes, declared_factories = (
                    _declared_public_python_symbols(source)
                )
                if (
                    selected_class is not None
                    and selected_class not in declared_classes
                ):
                    raise IsolatedSkillExecutorError(
                        "invalid_process_invocation",
                        "class_name is not a public top-level class declared by "
                        "the exact authorized script.",
                    )
                if (
                    selected_factory is not None
                    and selected_factory not in declared_factories
                ):
                    raise IsolatedSkillExecutorError(
                        "invalid_process_invocation",
                        "factory_name is not a public top-level function declared "
                        "by the exact authorized script.",
                    )
            elif constructor_args is not None or constructor_kwargs is not None:
                raise IsolatedSkillExecutorError(
                    "invalid_process_invocation",
                    "constructor arguments require class_name or factory_name.",
                )
            profile_selection = select_skill_runtime_profile(
                authority.snapshot,
                authority.script_resource,
            )
            if (
                profile_selection.package_sha256
                != authority.package_sha256
                or profile_selection.script_sha256
                != authority.script_sha256
            ):
                raise IsolatedSkillExecutorError(
                    "skill_runtime_profile_authority_mismatch",
                    "Runtime selection is not bound to the authorized Skill "
                    "snapshot.",
                )
            if (
                profile_selection.required_cwd is not None
                and cwd != profile_selection.required_cwd
            ):
                raise IsolatedSkillExecutorError(
                    "skill_runtime_cwd_mismatch",
                    "The exact Skill source uses a bare relative local "
                    "dispatch and requires cwd="
                    f"{profile_selection.required_cwd!r}; use an anchored "
                    "$CHATDS_SKILL_DIR path to make it cwd-independent.",
                )
            profile = profile_selection.runtime_profile
            runtime_binding = runtime_profile_socket_binding(profile)
            socket_path = runtime_binding.socket_path
            if selected_factory is not None:
                parameters = bind_python_invocation_parameters(
                    authority.snapshot.read_bytes(
                        authority.script_resource
                    ).decode("utf-8", errors="replace"),
                    callable_name=selected_factory,
                    positional=constructor_args,
                    keywords=constructor_kwargs,
                )
                proven_invocations = (
                    ({
                        "source": "python",
                        "callable": selected_factory,
                        "parameters": parameters,
                    },)
                    if parameters is not None
                    else ()
                )
            elif selected_class is not None:
                parameters = bind_python_invocation_parameters(
                    authority.snapshot.read_bytes(
                        authority.script_resource
                    ).decode("utf-8", errors="replace"),
                    callable_name=selected_class,
                    positional=constructor_args,
                    keywords=constructor_kwargs,
                )
                proven_invocations = (
                    ({
                        "source": "python",
                        "callable": selected_class,
                        "parameters": parameters,
                    },)
                    if parameters is not None
                    else ()
                )
            else:
                proven_invocations = ({
                    "source": "argv",
                    "args": args or [],
                },)
            egress_policy = (
                invocation_bound_skill_egress_policy_for_invocations(
                    context,
                    authority.skill_name,
                    profile_selection,
                    invocations=proven_invocations,
                )
            )
            require_execution_authority(
                context,
                boundary="skill_process.start.submit",
            )
            record, response = await _MANAGER.start(
                context=context,
                authority=authority,
                args=args,
                cwd=cwd,
                idle_ttl_seconds=idle_ttl_seconds,
                max_runtime_seconds=max_runtime_seconds,
                class_name=selected_class,
                factory_name=selected_factory,
                constructor_args=constructor_args,
                constructor_kwargs=constructor_kwargs,
                egress_rules=egress_policy.rule_payload(),
                private_origins=egress_policy.private_origins,
                runtime_profile=profile,
                socket_path=socket_path,
            )
            try:
                require_execution_authority(
                    context,
                    boundary="skill_process.start.publish",
                )
            except ExecutionAuthorityRevoked:
                await _MANAGER.cleanup_run(
                    context.user_id,
                    context.session_id,
                    str(context.run_id or context.browser_run_scope_id or ""),
                )
                raise
            if (
                response.get("runtime_profile")
                != runtime_binding.executor_runtime_profile
            ):
                # This cannot safely be converted into a base/native fallback.
                try:
                    await _MANAGER.close_explicit(record.process_id, context)
                except BaseException:
                    pass
                raise IsolatedSkillExecutorError(
                    "runtime_profile_mismatch",
                    "The isolated executor did not attest the runtime profile "
                    "selected from the exact Skill script.",
                )
            return _public_receipt("start", record.process_id, response)

        safe_process_id = _validate_process_id(process_id)
        if operation == "close":
            require_execution_authority(
                context,
                boundary="skill_process.close.submit",
            )
            (
                response,
                already_closed,
                process_evidence_receipt,
            ) = await _MANAGER.close_explicit(
                safe_process_id,
                context,
            )
            return _public_receipt(
                operation,
                safe_process_id,
                response,
                already_closed=already_closed,
                process_evidence_receipt=process_evidence_receipt,
            )

        record = await _MANAGER.acquire(safe_process_id, context)
        active_record = record
        async with record.lock:
            require_execution_authority(
                context,
                boundary=f"skill_process.{operation}.submit",
            )
            if operation == "write":
                content = _decode_stdin(data, data_base64)
                operation_id = _operation_uuid(context, "write")
                content_bytes = (
                    content.encode("utf-8", errors="strict")
                    if isinstance(content, str)
                    else content
                )
                fingerprint = _mutation_fingerprint(
                    "write",
                    {
                        "content_sha256": hashlib.sha256(
                            content_bytes
                        ).hexdigest(),
                        "size_bytes": len(content_bytes),
                    },
                )
                response = await _dispatch_mutating_operation(
                    record,
                    operation="write",
                    operation_id=operation_id,
                    fingerprint=fingerprint,
                    dispatch=lambda: write_isolated_process_stdin(
                        record.lease,
                        content,
                        op_id=operation_id,
                    ),
                )
                if (
                    record.lease.invocation_mode == "cli"
                    and isinstance(response.get("bytes_written"), int)
                    and response["bytes_written"] > 0
                ):
                    record.cli_semantic_task_bound = True
            elif operation == "stdin_close":
                operation_id = _operation_uuid(context, "stdin-close")
                response = await _dispatch_mutating_operation(
                    record,
                    operation="stdin_close",
                    operation_id=operation_id,
                    fingerprint=_mutation_fingerprint(
                        "stdin_close",
                        {},
                    ),
                    dispatch=lambda: close_isolated_process_stdin(
                        record.lease,
                        op_id=operation_id,
                    ),
                )
            elif operation == "read":
                operation_id = _operation_uuid(context, "read")
                response = await read_isolated_process_output(
                    record.lease,
                    stdout_offset=stdout_offset,
                    stderr_offset=stderr_offset,
                    max_bytes=max_bytes,
                    wait_ms=wait_ms,
                    op_id=operation_id,
                )
            elif operation == "call":
                selected_method = _safe_public_identifier(
                    method_name,
                    field_name="method_name",
                )
                if selected_method is None:
                    raise IsolatedSkillExecutorError(
                        "invalid_function_call",
                        "call requires method_name.",
                    )
                operation_id = _operation_uuid(context, "call")
                response = await _dispatch_mutating_operation(
                    record,
                    operation="call",
                    operation_id=operation_id,
                    fingerprint=_mutation_fingerprint(
                        "call",
                        {
                            "method_name": selected_method,
                            "method_args": method_args or [],
                            "method_kwargs": method_kwargs or {},
                        },
                    ),
                    dispatch=lambda: call_isolated_process_instance(
                        record.lease,
                        method_name=selected_method,
                        method_args=method_args,
                        method_kwargs=method_kwargs,
                        op_id=operation_id,
                    ),
                )
                call_id = response.get("call_id")
                if (
                    response.get("partial") is not False
                    or call_id != operation_id
                ):
                    raise IsolatedSkillExecutorError(
                        "invalid_response",
                        "The executor did not attest one complete runtime-issued "
                        "structured call identity.",
                    )
                call = _PendingStructuredCall(
                    call_id=operation_id,
                    method_name=selected_method,
                    semantic_task_bound=bool(
                        selected_method.casefold() != "main"
                        or method_args
                        or method_kwargs
                    ),
                )
                prior = record.structured_call_history.get(operation_id)
                if prior is not None and prior != call:
                    raise IsolatedSkillExecutorError(
                        "invalid_response",
                        "A replayed structured call changed its semantic identity.",
                    )
                record.structured_call_history[operation_id] = call
                if operation_id not in {
                    item.get("call_id")
                    for item in record.completed_process_receipts
                }:
                    record.pending_structured_calls[operation_id] = call
                record.latest_semantic_call_id = (
                    operation_id
                    if call.semantic_task_bound
                    else None
                )
            elif operation == "sync":
                operation_id = _operation_uuid(context, "sync")
                response = await _dispatch_mutating_operation(
                    record,
                    operation="sync",
                    operation_id=operation_id,
                    fingerprint=_mutation_fingerprint("sync", {}),
                    dispatch=lambda: sync_isolated_process_artifacts(
                        record.lease,
                        op_id=operation_id,
                        execution_authority_check=lambda: (
                            require_execution_authority(
                                context,
                                boundary="skill_process.sync.commit",
                            )
                        ),
                    ),
                )
            elif operation == "signal":
                selected_signal = str(signal or "")
                operation_id = _operation_uuid(context, "signal")
                response = await _dispatch_mutating_operation(
                    record,
                    operation="signal",
                    operation_id=operation_id,
                    fingerprint=_mutation_fingerprint(
                        "signal",
                        {"signal": selected_signal},
                    ),
                    dispatch=lambda: signal_isolated_process(
                        record.lease,
                        selected_signal,
                        op_id=operation_id,
                    ),
                )
            else:  # pragma: no cover - guarded by the operation set above
                raise AssertionError(operation)
            process_evidence_receipt = (
                _terminal_process_evidence_receipt(
                    record,
                    operation,
                    response,
                    operation_id=operation_id,
                )
                if operation in {"read", "sync"}
                else None
            )
        return _public_receipt(
            operation,
            safe_process_id,
            response,
            process_evidence_receipt=process_evidence_receipt,
        )
    except SessionSandboxPolicyError as exc:
        return _json_error(
            "invalid_session_sandbox_policy",
            str(exc),
            operation=str(operation or ""),
        )
    except ExecutionAuthorityRevoked:
        return _json_error(
            "execution_authority_revoked",
            "Delegated execution authority was revoked; the process operation "
            "was not submitted.",
            operation=str(operation or ""),
        )
    except IsolatedSkillExecutorError as exc:
        code = exc.code
        if terminal_process_lease_error_action(exc) is not None:
            if active_record is not None:
                await _MANAGER.forget_lost(active_record, exc)
            if exc.terminal_lease_state is None:
                code = "lease_lost"
        return _json_error(code, str(exc), operation=str(operation or ""))
    except (OSError, RuntimeError, ValueError) as exc:
        return _json_error(
            "skill_process_internal_error",
            "Persistent Skill execution failed safely "
            f"({type(exc).__name__}).",
            operation=str(operation or ""),
        )


async def cleanup_skill_process_root(
    user_id: str,
    session_id: str,
    root_run_id: str,
) -> dict[str, Any]:
    """Close only leases owned by one root AgentRun."""

    return await _MANAGER.cleanup_root(user_id, session_id, root_run_id)


async def cleanup_skill_process_run(
    user_id: str,
    session_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Close only persistent executor leases created by one exact run."""

    return await _MANAGER.cleanup_run(user_id, session_id, run_id)


async def cleanup_skill_process_session(
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Close every persistent Skill process in one session."""

    return await _MANAGER.cleanup_session(user_id, session_id)


async def close_all_skill_processes() -> dict[str, Any]:
    """Harness-shutdown cleanup."""

    return await _MANAGER.close_all()


RUN_SKILL_PROCESS_SCHEMA = {
    "name": "run_skill_process",
    "description": (
        "Start or operate one persistent isolated process declared by an exact "
        "installed Skill. start requires the full "
        "skills/<canonical-skill>/<relative-script> path already authorized for "
        "this compiled task and returns an opaque process_id. Use write/read for "
        "multi-turn JSONL or CLI stdin; use stdin_close to deliver EOF without "
        "terminating a CLI that must finish and flush output. Alternatively, "
        "initialize a Python object with one public class_name or factory_name "
        "exported by the exact script and use call for its public methods. call "
        "only enqueues one request and returns call_id; repeatedly read from the "
        "returned byte cursors until stdout contains the matching JSONL "
        "call_result. If that method wrote files, sync before read_file or image "
        "inspection. close performs the final sync and cleanup. Each validated "
        "artifact file is atomically replaced; a multi-file batch is recoverable "
        "and idempotent rather than an all-or-none filesystem transaction. Runtime "
        "profile, socket, owner scope, digests, and operation IDs are selected "
        "by the Harness and cannot be supplied here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "start", "write", "stdin_close", "read", "call", "sync",
                    "signal", "close",
                ],
                "description": "The bounded process operation.",
            },
            "script_path": {
                "type": "string",
                "maxLength": MAX_SCRIPT_PATH_CHARS,
                "pattern": r"^skills/",
                "description": (
                    "start only: exact canonical installed-Skill script path."
                ),
            },
            "process_id": {
                "type": "string",
                "maxLength": MAX_PROCESS_ID_CHARS,
                "pattern": r"^sp_[A-Za-z0-9_-]+$",
                "description": (
                    "All operations after start: opaque value returned by start."
                ),
            },
            "args": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_ARG_CHARS},
                "maxItems": MAX_ARGS,
                "description": "start only: literal CLI argv entries.",
            },
            "cwd": {
                "type": "string",
                "enum": ["workspace", "script", "skill"],
                "description": "start only: isolated working-directory policy.",
            },
            "idle_ttl_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PROCESS_LEASE_TTL_SECONDS,
                "description": "start only: idle lease lifetime.",
            },
            "max_runtime_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PROCESS_RUNTIME_SECONDS,
                "description": "start only: absolute process lifetime.",
            },
            "class_name": {
                "type": "string",
                "maxLength": 128,
                "pattern": r"^[A-Za-z][A-Za-z0-9_]*$",
                "description": (
                    "start only: public top-level Python class; mutually "
                    "exclusive with factory_name."
                ),
            },
            "factory_name": {
                "type": "string",
                "maxLength": 128,
                "pattern": r"^[A-Za-z][A-Za-z0-9_]*$",
                "description": (
                    "start only: public top-level Python factory; mutually "
                    "exclusive with class_name."
                ),
            },
            "constructor_args": {
                "type": "array",
                "maxItems": MAX_FUNCTION_ARGS,
                "description": "start only: bounded JSON constructor arguments.",
            },
            "constructor_kwargs": {
                "type": "object",
                "maxProperties": MAX_FUNCTION_KWARGS,
                "description": "start only: bounded JSON constructor keywords.",
            },
            "data": {
                "type": "string",
                "maxLength": MAX_PROCESS_STDIN_CHUNK_BYTES,
                "description": "write only: UTF-8 stdin chunk.",
            },
            "data_base64": {
                "type": "string",
                "maxLength": ((MAX_PROCESS_STDIN_CHUNK_BYTES + 2) // 3) * 4,
                "description": "write only: base64 stdin chunk.",
            },
            "method_name": {
                "type": "string",
                "maxLength": 128,
                "pattern": r"^[A-Za-z][A-Za-z0-9_]*$",
                "description": (
                    "call only: public instance method. The operation enqueues "
                    "the method; retrieve its matching call_result with read."
                ),
            },
            "method_args": {
                "type": "array",
                "maxItems": MAX_FUNCTION_ARGS,
                "description": (
                    f"call only: JSON arguments, at most {MAX_PROCESS_CALL_BYTES} "
                    "encoded bytes with method_kwargs."
                ),
            },
            "method_kwargs": {
                "type": "object",
                "maxProperties": MAX_FUNCTION_KWARGS,
                "description": "call only: JSON keyword arguments.",
            },
            "stdout_offset": {
                "type": "integer",
                "minimum": 0,
                "description": "read only: next stdout byte cursor.",
            },
            "stderr_offset": {
                "type": "integer",
                "minimum": 0,
                "description": "read only: next stderr byte cursor.",
            },
            "max_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PROCESS_READ_BYTES,
                "description": "read only: maximum bytes per stream.",
            },
            "wait_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_PROCESS_READ_WAIT_MS,
                "description": "read only: bounded long-poll duration.",
            },
            "signal": {
                "type": "string",
                "enum": ["interrupt", "terminate", "kill"],
                "description": "signal only: process-group signal.",
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
}
