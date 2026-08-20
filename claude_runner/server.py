"""Trusted Docker supervisor for isolated Claude Code Turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

try:
    import docker
    from docker.errors import DockerException, ImageNotFound, NotFound
except ModuleNotFoundError:  # pragma: no cover - production image pins Docker SDK
    docker = None

    class DockerException(Exception):
        pass

    class ImageNotFound(DockerException):
        pass

    class NotFound(DockerException):
        pass
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from .config import ProviderProfile, RunnerSettings, load_settings
from .policy import compile_turn_egress_policy, verify_skill_view
from .runtime_capabilities import compile_runtime_capability_contract
from .native_control import (
    build_native_updated_input,
    is_native_user_interaction,
    native_user_interaction_kind,
)
from .input_attachments import (
    INPUT_ATTACHMENT_DIRECTORY,
    INPUT_ATTACHMENT_PATH,
    MAX_INPUT_ATTACHMENTS,
    verify_input_attachments as _verify_input_attachments,
)
from .mcp_schedule_control import normalize_schedule_capability_aliases
from workspace_lock import workspace_mutation_guard
from native_security.workflow_contract import (
    compile_turn_workflow_contract as compile_native_workflow_contract,
)


logger = logging.getLogger(__name__)
SAFE_ID = re.compile(r"^[0-9a-f]{32}$")
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_ENTRIES = 200_000
SECCOMP_PROFILE_PATH = Path("/app/claude_runner/seccomp_profile.json")
SETID_STRIPPED_LABEL = "org.opencontainers.image.chatds.setid-stripped"
EGRESS_POLICY_LABEL = "org.opencontainers.image.chatds.egress-policy"
RUNNER_RUNTIME_LABEL = "org.opencontainers.image.chatds.runner-runtime"
EXPECTED_EGRESS_POLICY_RUNTIME = "signed-public-read-v1"
EXPECTED_RUNNER_RUNTIME = "installed-isolated-package-v1"
RUNNER_IMAGE_SELF_TEST_ARGUMENT = "--chatds-image-self-test"
RUNNER_IMAGE_SELF_TEST_SCHEMA = "chatds.claude-runner-image-self-test.v1"
EXPECTED_SKILL_PLUGIN_NAME = "chatds-session-skills"
_STATUS_UPDATE_LOCK = threading.RLock()


class _PreflightCancelled(RuntimeError):
    """Internal fence: accepted work was revoked before Docker authority."""


class _PreflightTimedOut(RuntimeError):
    """Internal fence: bounded Skill/storage preparation did not converge."""


class StartRunRequest(BaseModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    root_run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    conversation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    model_id: str = Field(min_length=1, max_length=128)
    api_model: str = Field(min_length=1, max_length=128)
    provider_profile: str = Field(min_length=1, max_length=64)
    provider_base_url: str = Field(min_length=8, max_length=2048)
    provider_protocol: str = Field(max_length=32)
    messages: list[dict[str, Any]] = Field(max_length=4096)
    input_attachments: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_INPUT_ATTACHMENTS,
    )
    max_output_tokens: int = Field(ge=1, le=262144)
    context_window_tokens: int = Field(ge=200_000, le=4_000_000)
    workspace_path: str = Field(min_length=1, max_length=4096)
    skill_view_path: str = Field(min_length=1, max_length=4096)
    skill_view_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_session_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )
    resume_from_native_session_id: str | None = Field(
        default=None,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    )
    source: str = Field(default="chat", max_length=24)
    user_turn_text: str = Field(default="", max_length=2_000_000)
    permission_preset: Literal[
        "read_only", "workspace_write", "session_full"
    ] = "workspace_write"

    @model_validator(mode="after")
    def bounded_payload(self):
        encoded = json.dumps(
            self.model_dump(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("Claude run request is too large")
        return self


class RunIdentityRequest(BaseModel):
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    conversation_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class ApprovalDecisionRequest(RunIdentityRequest):
    model_config = {"extra": "forbid"}

    request_seq: int = Field(ge=1)
    decision: Literal["allow", "deny"]
    answers: dict[str, str] | None = None

    @model_validator(mode="after")
    def bounded_answers(self):
        if self.answers is not None and (
            not 1 <= len(self.answers) <= 4
            or any(
                not key
                or len(key) > 4_000
                or not value.strip()
                or len(value) > 4_000
                or "\x00" in key
                or "\x00" in value
                for key, value in self.answers.items()
            )
        ):
            raise ValueError("Native question answers are invalid")
        return self


class SessionIdentityRequest(BaseModel):
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class RunManager:
    def __init__(self, settings: RunnerSettings, client) -> None:
        self.settings = settings
        self.client = client
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_runs)
        self._preflight_semaphore = asyncio.Semaphore(
            settings.max_concurrent_runs
        )
        self._preflight_executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_runs,
            thread_name_prefix="claude-preflight",
        )
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._guard = asyncio.Lock()
        self._draining = False
        self._revoked_sessions: set[tuple[str, str]] = set()
        self._index_root = settings.state_root / "run-index"
        self._index_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._index_root, 0o700)
        self._admission_root = settings.state_root / "admissions"
        self._admission_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._admission_root, 0o700)

    async def start(self, request: StartRunRequest) -> dict[str, Any]:
        # Provider identity is deployment-owned and does not touch Session/NFS
        # state, so reject an invalid binding before accepting durable work.
        profile = self._provider(request)
        async with self._guard:
            if (request.user_id, request.conversation_id) in self._revoked_sessions:
                raise HTTPException(410, "Session execution has been revoked")
            admission, idempotent = self._ensure_admission(request)
            status = _read_json(admission / "status.json")
            existing_task = self._tasks.get(request.run_id)
            if (
                str(status.get("status") or "") not in TERMINAL_STATUSES
                and (existing_task is None or existing_task.done())
            ):
                task = asyncio.create_task(
                    self._prepare_and_execute(request, profile),
                    name=f"claude-prepare-{request.run_id}",
                )
                self._tasks[request.run_id] = task
                task.add_done_callback(
                    lambda _task, run_id=request.run_id: self._task_done(run_id)
                )
            return {
                "accepted": True,
                "idempotent": idempotent,
                "run_id": request.run_id,
                "native_session_id": request.native_session_id,
                "status": str(status.get("status") or "queued"),
                "phase": str(status.get("phase") or "preflight"),
            }

    def _prepare_run_sync(
        self,
        request: StartRunRequest,
        profile: ProviderProfile,
    ) -> tuple[Path, Path, Path, Path, Path, ProviderProfile]:
        workspace, skill_view, state = self._validate_paths(request)
        if self._admission_cancelled(request.run_id):
            raise _PreflightCancelled
        native_session_id = request.native_session_id
        run_dir = state / "control" / "runs" / request.run_id
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        request_path = run_dir / "request.json"
        status_path = run_dir / "status.json"
        resume_from_native_session_id = (
            request.resume_from_native_session_id
            if request.resume_from_native_session_id
            and _native_transcript_exists(
                state, request.resume_from_native_session_id
            )
            else None
        )
        skill_view_receipt = verify_skill_view(
            skill_view,
            request.skill_view_sha256,
        )
        if self._admission_cancelled(request.run_id):
            raise _PreflightCancelled
        _validate_native_skill_manifest(skill_view_receipt.manifest)
        _verify_input_attachments(
            messages=request.messages,
            attachments=request.input_attachments,
            workspace=workspace,
        )
        prompt = _build_prompt(
            request.messages,
            resume=resume_from_native_session_id is not None,
        )
        turn_skill_binding = _fresh_session_skill_binding(
            skill_view_receipt.manifest,
            resume=resume_from_native_session_id is not None,
        )
        workflow_contract = _compile_turn_workflow_contract(
            manifest=skill_view_receipt.manifest,
            user_turn_text=request.user_turn_text,
            bound_skill_name=(
                turn_skill_binding["skill_name"]
                if turn_skill_binding is not None
                else None
            ),
        )
        policy = compile_turn_egress_policy(
            skill_view_root=skill_view,
            skill_view_sha256=request.skill_view_sha256,
            verified_skill_view=skill_view_receipt,
            user_turn_text=request.user_turn_text,
            provider_base_url=profile.claude_base_url,
            configured_private_origins=self.settings.private_origin_allowlist,
            budget_scope_sha256=_scope_digest(
                "budget", request.user_id, request.conversation_id, request.root_run_id
            ),
            call_id_sha256=_scope_digest("call", request.root_run_id, request.run_id),
            limits=dict(self.settings.egress_limits),
            public_read_enabled=(
                self.settings.public_read_egress_enabled
            ),
        )
        runtime_capability_contract = compile_runtime_capability_contract(
            manifest=skill_view_receipt.manifest,
            egress_policy=policy,
        )
        raw_schedule_capability_aliases = skill_view_receipt.manifest.get(
            "schedule_capability_aliases",
            {},
        )
        schedule_capability_aliases = (
            normalize_schedule_capability_aliases(
                raw_schedule_capability_aliases
            )
        )
        if self._admission_cancelled(request.run_id):
            raise _PreflightCancelled
        sanitized = {
            "schema": "chatds.claude-run.v1",
            "run_id": request.run_id,
            "root_run_id": request.root_run_id,
            "user_id": request.user_id,
            "conversation_id": request.conversation_id,
            "model_id": request.model_id,
            "api_model": request.api_model,
            "provider_profile": profile.id,
            "provider_backend_base_url": profile.backend_base_url,
            "provider_claude_base_url": profile.claude_base_url,
            "provider_protocol": request.provider_protocol,
            "native_web_tools": profile.native_web_tools,
            "native_session_id": native_session_id,
            "resume_from_native_session_id": resume_from_native_session_id,
            "max_output_tokens": request.max_output_tokens,
            "context_window_tokens": request.context_window_tokens,
            "prompt": prompt,
            "input_attachments": request.input_attachments,
            "workspace_path": str(workspace),
            "skill_view_path": str(skill_view),
            "skill_view_sha256": request.skill_view_sha256,
            # A Session upload is an explicit native capability attachment.
            # On a fresh native Session, one unambiguous Session-level primary
            # is lowered to Claude Code's own slash-command path.  Ambient
            # user Skills, multiple primaries and resumed Turns remain under
            # Claude's native relevance router, avoiding stale reinjection.
            "turn_skill_binding": turn_skill_binding,
            # These compiler-owned values are part of the immutable Skill-view
            # digest.  The worker consumes them as control-plane data rather
            # than asking model prose to attest its own workflow/artifacts.
            "artifact_contracts": skill_view_receipt.manifest.get(
                "artifact_contracts", []
            ),
            "workflow_contract": workflow_contract,
            "runtime_requirements": skill_view_receipt.manifest.get(
                "runtime_requirements", []
            ),
            "runtime_capability_contract": runtime_capability_contract,
            "schedule_capability_aliases": schedule_capability_aliases,
            "skill_diagnostics": skill_view_receipt.manifest.get(
                "skill_diagnostics", []
            ),
            "source": request.source,
            "permission_preset": request.permission_preset,
            "egress_policy": policy,
        }
        digest = _canonical_sha256(sanitized)
        sanitized["request_sha256"] = digest
        if request_path.exists():
            existing = _read_json(request_path)
            if existing.get("request_sha256") != digest:
                raise HTTPException(409, "Run id already belongs to a different request")
            _read_json(status_path)
            return workspace, skill_view, state, run_dir, status_path, profile
        _atomic_json(request_path, sanitized, mode=0o600)
        _atomic_json(status_path, {
            "schema": "chatds.claude-run-status.v1",
            "run_id": request.run_id,
            "user_id": request.user_id,
            "conversation_id": request.conversation_id,
            "status": "queued",
            "phase": "queued",
            "container_id": None,
            "created_at_unix_ms": int(time.time() * 1000),
        }, mode=0o600)
        return workspace, skill_view, state, run_dir, status_path, profile

    async def _prepare_and_execute(
        self,
        request: StartRunRequest,
        profile: ProviderProfile,
    ) -> None:
        admission = self._admission_dir(request.run_id)
        started = time.monotonic()
        try:
            async def prepare():
                async with self._preflight_semaphore:
                    if self._admission_cancelled(request.run_id):
                        raise _PreflightCancelled
                    return await asyncio.get_running_loop().run_in_executor(
                        self._preflight_executor,
                        self._prepare_run_sync,
                        request,
                        profile,
                    )

            try:
                prepared = await asyncio.wait_for(
                    prepare(),
                    timeout=self.settings.preflight_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                # The kernel thread may remain inside an uninterruptible NFS
                # syscall. Revoke locally before publishing the terminal; a
                # late return will fail every remaining authority fence.
                _update_status(
                    admission / "status.json", cancellation_requested=True
                )
                raise _PreflightTimedOut from exc
            workspace, skill_view, state, run_dir, status_path, profile = prepared
            if self._admission_cancelled(request.run_id):
                await asyncio.to_thread(
                    _ensure_terminal_event,
                    run_dir / "events.jsonl",
                    status="cancelled",
                    error=None,
                )
                await asyncio.to_thread(
                    _update_status,
                    status_path,
                    status="cancelled",
                    phase="terminal",
                    container_id=None,
                    cancellation_requested=True,
                )
                return
            _update_status(
                admission / "status.json",
                status="queued",
                phase="queued",
                preflight_elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            await self._execute(
                request=request,
                workspace=workspace,
                skill_view=skill_view,
                state=state,
                run_dir=run_dir,
                request_path=run_dir / "request.json",
                status_path=status_path,
                profile=profile,
            )
            terminal = await asyncio.to_thread(
                _terminal_status, run_dir / "events.jsonl"
            )
            if terminal is not None:
                _update_status(
                    admission / "status.json",
                    status=terminal,
                    phase="terminal",
                )
        except _PreflightCancelled:
            _ensure_terminal_event(
                admission / "events.jsonl", status="cancelled", error=None
            )
            _update_status(
                admission / "status.json",
                status="cancelled",
                phase="terminal",
                cancellation_requested=True,
                preflight_elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except _PreflightTimedOut:
            _ensure_terminal_event(
                admission / "events.jsonl",
                status="failed",
                error="preflight_timeout",
            )
            _update_status(
                admission / "status.json",
                status="failed",
                phase="terminal",
                error="preflight_timeout",
                cancellation_requested=True,
                preflight_elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except asyncio.CancelledError:
            if self._draining:
                raise
            if not (admission / "events.jsonl").exists():
                _ensure_terminal_event(
                    admission / "events.jsonl", status="cancelled", error=None
                )
            _update_status(
                admission / "status.json",
                status="cancelled",
                phase="terminal",
                cancellation_requested=True,
            )
            raise
        except BaseException as exc:
            logger.exception(
                "Claude run preflight failed closed run=%s error_type=%s",
                request.run_id,
                type(exc).__name__,
            )
            _ensure_terminal_event(
                admission / "events.jsonl",
                status="failed",
                error="preflight_" + type(exc).__name__,
            )
            _update_status(
                admission / "status.json",
                status="failed",
                phase="terminal",
                error="preflight_" + type(exc).__name__,
                preflight_elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    def _task_done(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None and task.done():
            try:
                if not task.cancelled():
                    task.result()
            except asyncio.CancelledError:
                pass
            except BaseException:
                logger.exception("Claude run supervisor task failed run=%s", run_id)
            finally:
                self._tasks.pop(run_id, None)

    def _locator_path(self, run_id: str) -> Path:
        if not SAFE_ID.fullmatch(run_id):
            raise HTTPException(404, "Run not found")
        return self._index_root / f"{run_id}.json"

    def _admission_dir(self, run_id: str) -> Path:
        if not SAFE_ID.fullmatch(run_id):
            raise HTTPException(404, "Run not found")
        return self._admission_root / run_id

    def _ensure_admission(
        self, request: StartRunRequest
    ) -> tuple[Path, bool]:
        """Durably accept a start without touching Session/NFS storage.

        The local Supervisor volume is the control-plane pending-write journal.
        It contains no provider credential and makes the POST idempotent before
        slow immutable-view attestation begins in an isolated worker thread.
        """

        path = self._locator_path(request.run_id)
        request_payload = request.model_dump(mode="json")
        request_sha256 = _canonical_sha256(request_payload)
        admission = self._admission_dir(request.run_id)
        value = {
            "schema": "chatds.claude-run-locator.v2",
            "run_id": request.run_id,
            "user_id": request.user_id,
            "conversation_id": request.conversation_id,
            "request_sha256": request_sha256,
        }
        if path.exists():
            existing = _read_json(path)
            if existing != value:
                raise HTTPException(409, "Run locator identity conflict")
            durable_request = _read_json(admission / "request.json")
            if _canonical_sha256(durable_request) != request_sha256:
                raise HTTPException(409, "Run id already belongs to a different request")
            _read_json(admission / "status.json")
            return admission, True

        # A process can stop after both admission files fsync but before the
        # locator rename. Recover that pending write by exact request identity;
        # never overwrite a partial or conflicting admission directory.
        if admission.exists():
            durable_request = _read_json(admission / "request.json")
            durable_status = _read_json(admission / "status.json")
            if (
                _canonical_sha256(durable_request) != request_sha256
                or durable_status.get("run_id") != request.run_id
                or durable_status.get("user_id") != request.user_id
                or durable_status.get("conversation_id")
                != request.conversation_id
            ):
                raise HTTPException(409, "Run admission identity conflict")
            _atomic_create_json(path, value, mode=0o600)
            return admission, True

        try:
            admission.mkdir(mode=0o700)
            _atomic_json(admission / "request.json", request_payload, mode=0o600)
            _atomic_json(admission / "status.json", {
                "schema": "chatds.claude-admission-status.v1",
                "run_id": request.run_id,
                "user_id": request.user_id,
                "conversation_id": request.conversation_id,
                "status": "queued",
                "phase": "preflight",
                "cancellation_requested": False,
                "created_at_unix_ms": int(time.time() * 1000),
            }, mode=0o600)
            _atomic_create_json(path, value, mode=0o600)
        except BaseException:
            if not path.exists():
                shutil.rmtree(admission, ignore_errors=True)
            raise
        return admission, False

    def _admission_cancelled(self, run_id: str) -> bool:
        try:
            status = _read_json(self._admission_dir(run_id) / "status.json")
        except HTTPException:
            return False
        return bool(status.get("cancellation_requested"))

    def _read_run_locator(self, run_id: str) -> dict[str, str]:
        value = _read_json(self._locator_path(run_id))
        if (
            value.get("schema") not in {
                "chatds.claude-run-locator.v1",
                "chatds.claude-run-locator.v2",
            }
            or value.get("run_id") != run_id
            or not SAFE_ID.fullmatch(str(value.get("user_id") or ""))
            or not SAFE_ID.fullmatch(str(value.get("conversation_id") or ""))
        ):
            raise HTTPException(404, "Run state is unavailable")
        result = {
            "run_id": run_id,
            "user_id": str(value["user_id"]),
            "conversation_id": str(value["conversation_id"]),
        }
        if value.get("schema") == "chatds.claude-run-locator.v2":
            digest = str(value.get("request_sha256") or "")
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise HTTPException(404, "Run state is unavailable")
            result["request_sha256"] = digest
            result["schema"] = "chatds.claude-run-locator.v2"
        else:
            result["schema"] = "chatds.claude-run-locator.v1"
        return result

    async def reconcile_existing_containers(self) -> dict[str, int]:
        """Adopt trusted per-Turn containers after a Supervisor restart."""

        requeued_preflight = 0
        for locator_path in self._index_root.glob("*.json"):
            run_id = locator_path.stem
            try:
                locator = self._read_run_locator(run_id)
                if locator.get("schema") != "chatds.claude-run-locator.v2":
                    continue
                admission = self._admission_dir(run_id)
                status = _read_json(admission / "status.json")
                if (
                    str(status.get("phase") or "") != "preflight"
                    or str(status.get("status") or "") in TERMINAL_STATUSES
                    or status.get("cancellation_requested")
                ):
                    continue
                request = StartRunRequest.model_validate(
                    _read_json(admission / "request.json")
                )
                profile = self._provider(request)
            except (HTTPException, OSError, ValueError, TypeError):
                continue
            task = asyncio.create_task(
                self._prepare_and_execute(request, profile),
                name=f"claude-recover-preflight-{run_id}",
            )
            self._tasks[run_id] = task
            task.add_done_callback(
                lambda _task, recovered_run_id=run_id: self._task_done(
                    recovered_run_id
                )
            )
            requeued_preflight += 1

        containers = await asyncio.to_thread(
            self.client.containers.list,
            all=True,
            filters={"label": "chatds.component=claude-runner"},
        )
        adopted = 0
        removed_unknown = 0
        active_container_runs: set[str] = set()
        for container in containers:
            run_id = str((container.labels or {}).get("chatds.run_id") or "")
            try:
                locator = self._read_run_locator(run_id)
                run_dir = self._run_dir(
                    locator["user_id"], locator["conversation_id"], run_id
                )
                request = _read_json(run_dir / "request.json")
                status_path = run_dir / "status.json"
                status = _read_json(status_path)
                if (
                    request.get("user_id") != locator["user_id"]
                    or request.get("conversation_id") != locator["conversation_id"]
                    or status.get("user_id") != locator["user_id"]
                    or status.get("conversation_id") != locator["conversation_id"]
                ):
                    raise HTTPException(404, "Run identity mismatch")
            except (HTTPException, OSError, ValueError, TypeError):
                await asyncio.to_thread(container.remove, force=True)
                removed_unknown += 1
                continue
            terminal = _terminal_status(run_dir / "events.jsonl")
            if terminal is not None or str(status.get("status") or "") in TERMINAL_STATUSES:
                await asyncio.to_thread(container.remove, force=True)
                _update_status(
                    status_path,
                    status=terminal or str(status.get("status") or "failed"),
                    phase="terminal",
                    container_id=None,
                )
                continue
            active_container_runs.add(run_id)
            _update_status(
                status_path,
                status="running",
                phase="running",
                container_id=container.id,
            )
            created_ms = int(status.get("created_at_unix_ms") or int(time.time() * 1000))
            elapsed = max(0.0, time.time() - created_ms / 1000.0)
            remaining = max(0.0, self.settings.max_run_seconds - elapsed)
            task = asyncio.create_task(
                self._adopt_existing_container(
                    container,
                    run_id=run_id,
                    run_dir=run_dir,
                    status_path=status_path,
                    remaining_seconds=remaining,
                ),
                name=f"claude-adopt-{run_id}",
            )
            self._tasks[run_id] = task
            task.add_done_callback(
                lambda _task, adopted_run_id=run_id: self._task_done(adopted_run_id)
            )
            adopted += 1

        failed_orphans = 0
        requeued = 0
        for locator_path in self._index_root.glob("*.json"):
            run_id = locator_path.stem
            if not SAFE_ID.fullmatch(run_id) or run_id in active_container_runs:
                continue
            try:
                locator = self._read_run_locator(run_id)
                run_dir = self._run_dir(
                    locator["user_id"], locator["conversation_id"], run_id
                )
                status_path = run_dir / "status.json"
                status = _read_json(status_path)
            except (HTTPException, OSError, ValueError, TypeError):
                continue
            terminal = _terminal_status(run_dir / "events.jsonl")
            current_status = str(status.get("status") or "")
            if terminal is not None or current_status in TERMINAL_STATUSES:
                continue
            if status.get("cancellation_requested"):
                _ensure_terminal_event(
                    run_dir / "events.jsonl",
                    status="cancelled",
                    error=None,
                )
                _update_status(
                    status_path,
                    status="cancelled",
                    phase="terminal",
                    container_id=None,
                )
                continue
            if current_status in {"queued", "starting"}:
                try:
                    request = _recover_start_request(run_dir / "request.json")
                    workspace, skill_view, state = self._validate_paths(request)
                    profile = self._provider(request)
                except (HTTPException, OSError, ValueError, TypeError):
                    request = None
                if request is not None:
                    _update_status(
                        status_path,
                        status="queued",
                        phase="queued",
                        container_id=None,
                        recovered_after_restart=True,
                    )
                    task = asyncio.create_task(
                        self._execute(
                            request=request,
                            workspace=workspace,
                            skill_view=skill_view,
                            state=state,
                            run_dir=run_dir,
                            request_path=run_dir / "request.json",
                            status_path=status_path,
                            profile=profile,
                        ),
                        name=f"claude-requeue-{run_id}",
                    )
                    self._tasks[run_id] = task
                    task.add_done_callback(
                        lambda _task, recovered_run_id=run_id: self._task_done(
                            recovered_run_id
                        )
                    )
                    requeued += 1
                    continue
            if current_status not in TERMINAL_STATUSES:
                _ensure_terminal_event(
                    run_dir / "events.jsonl",
                    status="failed",
                    error="supervisor_restart_before_container_adoption",
                )
                _update_status(
                    status_path,
                    status="failed",
                    phase="terminal",
                    container_id=None,
                )
                failed_orphans += 1
        return {
            "adopted": adopted,
            "removed_unknown": removed_unknown,
            "requeued_preflight": requeued_preflight,
            "requeued": requeued,
            "failed_orphans": failed_orphans,
        }

    async def _adopt_existing_container(
        self,
        container,
        *,
        run_id: str,
        run_dir: Path,
        status_path: Path,
        remaining_seconds: float,
    ) -> None:
        try:
            await self._monitor_container(
                container,
                run_id=run_id,
                run_dir=run_dir,
                status_path=status_path,
                remaining_seconds=remaining_seconds,
            )
        except asyncio.CancelledError:
            if self._draining:
                raise
            _ensure_terminal_event(
                run_dir / "events.jsonl",
                status="cancelled",
                error=None,
            )
            _update_status(
                status_path,
                status="cancelled",
                phase="terminal",
                container_id=None,
            )
            raise
        except Exception as exc:
            logger.exception(
                "Adopted Claude run failed closed run=%s error_type=%s",
                run_id,
                type(exc).__name__,
            )
            _ensure_terminal_event(
                run_dir / "events.jsonl",
                status="failed",
                error=type(exc).__name__,
            )
            _update_status(
                status_path,
                status="failed",
                phase="terminal",
                container_id=None,
            )

    async def _execute(
        self,
        *,
        request: StartRunRequest,
        workspace: Path,
        skill_view: Path,
        state: Path,
        run_dir: Path,
        request_path: Path,
        status_path: Path,
        profile: ProviderProfile,
    ) -> None:
        try:
            async with self._semaphore:
                # The authoritative cancellation fence is local and cheap.
                # NFS status mutation is performed off the HTTP event loop so
                # a storage stall cannot take health/cancel endpoints down.
                async with self._guard:
                    cancelled = (
                        self._admission_cancelled(request.run_id)
                        or (request.user_id, request.conversation_id)
                        in self._revoked_sessions
                    )
                    if not cancelled:
                        _update_status(
                            self._admission_dir(request.run_id) / "status.json",
                            status="starting",
                            phase="starting",
                        )
                can_start = await asyncio.to_thread(
                    self._mark_starting_sync,
                    run_dir,
                    status_path,
                    cancelled,
                )
                if not can_start:
                    return
                container = await asyncio.to_thread(
                    self._create_container_sync,
                    request,
                    workspace,
                    skill_view,
                    state,
                    run_dir,
                    request_path,
                    status_path,
                    profile,
                )
                if container is None:
                    return
                await self._monitor_container(
                    container,
                    run_id=request.run_id,
                    run_dir=run_dir,
                    status_path=status_path,
                    remaining_seconds=float(self.settings.max_run_seconds),
                )
        except asyncio.CancelledError:
            if self._draining:
                # A Supervisor restart is not a user cancellation. Docker owns
                # any already-created Turn and the next Supervisor adopts it;
                # a durable queued request is re-enqueued before serving.
                raise
            await asyncio.to_thread(
                self._finish_run_sync,
                run_dir,
                status_path,
                "cancelled",
                None,
            )
            raise
        except BaseException as exc:
            logger.exception(
                "Claude run failed closed run=%s error_type=%s",
                request.run_id,
                type(exc).__name__,
            )
            await asyncio.to_thread(
                self._finish_run_sync,
                run_dir,
                status_path,
                "failed",
                type(exc).__name__,
            )

    def _mark_starting_sync(
        self,
        run_dir: Path,
        status_path: Path,
        cancelled: bool,
    ) -> bool:
        status = _read_json(status_path)
        if str(status.get("status") or "") in TERMINAL_STATUSES:
            return False
        if cancelled or status.get("cancellation_requested"):
            self._finish_run_sync(run_dir, status_path, "cancelled", None)
            return False
        _update_status(
            status_path,
            status="starting",
            phase="starting",
            container_id=None,
        )
        return True

    @staticmethod
    def _finish_run_sync(
        run_dir: Path,
        status_path: Path,
        status: str,
        error: str | None,
    ) -> None:
        _ensure_terminal_event(
            run_dir / "events.jsonl", status=status, error=error
        )
        changes: dict[str, Any] = {
            "status": status,
            "phase": "terminal",
            "container_id": None,
        }
        if error is not None:
            changes["error"] = error
        if status == "cancelled":
            changes["cancellation_requested"] = True
        _update_status(status_path, **changes)

    def _create_container_sync(
        self,
        request: StartRunRequest,
        workspace: Path,
        skill_view: Path,
        state: Path,
        run_dir: Path,
        request_path: Path,
        status_path: Path,
        profile: ProviderProfile,
    ):
        with _prepared_worker_workspace(workspace, self.settings.worker_gid):
            if (
                self._admission_cancelled(request.run_id)
                or _read_json(status_path).get("cancellation_requested")
            ):
                _ensure_terminal_event(
                    run_dir / "events.jsonl",
                    status="cancelled",
                    error=None,
                )
                _update_status(
                    status_path,
                    status="cancelled",
                    phase="terminal",
                    container_id=None,
                )
                return None
            home = state / "home"
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(home, self.settings.worker_uid, self.settings.worker_gid)
            container_name = "chatds-claude-" + request.run_id
            environment = {
                "CHATDS_RUN_CONFIG": "/run/chatds/request.json",
                "CHATDS_EVENT_LEDGER": f"/state/control/runs/{request.run_id}/events.jsonl",
                "CLAUDE_PROVIDER_API_KEY": profile.api_key,
                "SKILL_EGRESS_POLICY_TOKEN": os.environ["SKILL_EGRESS_POLICY_TOKEN"],
                "CLAUDE_RUNNER_WORKER_UID": str(self.settings.worker_uid),
                "CLAUDE_RUNNER_WORKER_GID": str(self.settings.worker_gid),
            }
            labels = {
                "chatds.component": "claude-runner",
                "chatds.run_id": request.run_id,
                "chatds.user_sha256": hashlib.sha256(request.user_id.encode()).hexdigest(),
                "chatds.conversation_sha256": hashlib.sha256(
                    request.conversation_id.encode()
                ).hexdigest(),
            }
            volumes = {
                str(workspace): {
                    "bind": "/workspace",
                    "mode": (
                        "ro" if request.permission_preset == "read_only" else "rw"
                    ),
                },
                str(state): {"bind": "/state", "mode": "rw"},
                str(skill_view): {"bind": "/skill-view", "mode": "ro"},
                str(request_path): {"bind": "/run/chatds/request.json", "mode": "ro"},
                self.settings.egress_proxy_volume: {
                    "bind": "/run/chatds-skill-egress", "mode": "ro"
                },
                self.settings.workspace_lock_volume: {
                    "bind": "/run/chatds-workspace-lock-plane", "mode": "rw"
                },
            }
            if request.input_attachments:
                attachment_root = workspace / INPUT_ATTACHMENT_DIRECTORY
                volumes[str(attachment_root)] = {
                    "bind": f"/workspace/{INPUT_ATTACHMENT_DIRECTORY}",
                    "mode": "ro",
                }
            try:
                container = self.client.containers.run(
                    self.settings.runner_image,
                    name=container_name,
                    detach=True,
                    network_mode="none",
                    read_only=True,
                    user="0:0",
                    group_add=["65530"],
                    cap_drop=["ALL"],
                    cap_add=["CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID"],
                    security_opt=_runner_security_options(
                        self.settings.security_mode
                    ),
                    pids_limit=512,
                    mem_limit=os.environ.get("CLAUDE_RUNNER_MEMORY_LIMIT", "6g"),
                    nano_cpus=int(float(os.environ.get("CLAUDE_RUNNER_CPUS", "4")) * 1_000_000_000),
                    tmpfs={
                        "/tmp": "rw,noexec,nosuid,nodev,size=2g,mode=1777",
                        "/runtime": "rw,nosuid,nodev,size=64m,mode=0755",
                        "/dev/shm": "rw,nosuid,nodev,size=1g,mode=1777",
                    },
                    volumes=volumes,
                    environment=environment,
                    labels=labels,
                )
            except DockerException as exc:
                raise RuntimeError("runner_container_start_failed") from exc
            _update_status(
                status_path,
                status="running",
                phase="running",
                container_id=container.id,
            )
            return container

    async def _monitor_container(
        self,
        container,
        *,
        run_id: str,
        run_dir: Path,
        status_path: Path,
        remaining_seconds: float,
    ) -> None:
        deadline = time.monotonic() + max(0.0, remaining_seconds)
        exit_code: int | None = None
        disappeared = False
        detach_for_restart = False
        try:
            while time.monotonic() < deadline:
                try:
                    await asyncio.to_thread(container.reload)
                except NotFound:
                    disappeared = True
                    break
                if container.status in {"exited", "dead"}:
                    result = await asyncio.to_thread(container.wait, timeout=10)
                    exit_code = int(result.get("StatusCode", 1))
                    break
                await asyncio.sleep(1.0)
            if exit_code is None and not disappeared:
                try:
                    await asyncio.to_thread(container.kill, signal="SIGUSR1")
                except (NotFound, DockerException):
                    pass
                try:
                    result = await asyncio.to_thread(container.wait, timeout=40)
                    exit_code = int(result.get("StatusCode", 124))
                except DockerException:
                    try:
                        await asyncio.to_thread(container.stop, timeout=30)
                    except (NotFound, DockerException):
                        pass
                    exit_code = 124
                _ensure_terminal_event(
                    run_dir / "events.jsonl",
                    status="failed",
                    error="run_hard_timeout",
                )
        except asyncio.CancelledError:
            if self._draining:
                detach_for_restart = True
            raise
        finally:
            if not detach_for_restart:
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except NotFound:
                    pass
                except DockerException:
                    logger.warning("Could not remove Claude runner container run=%s", run_id)
        persisted_terminal = _terminal_event(run_dir / "events.jsonl")
        terminal = (
            str(persisted_terminal.get("status"))
            if persisted_terminal is not None
            else None
        )
        error: str | None = None
        error_stage: str | None = None
        if persisted_terminal is not None:
            candidate_error = persisted_terminal.get("error")
            candidate_stage = persisted_terminal.get("error_stage")
            if (
                isinstance(candidate_error, str)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", candidate_error)
            ):
                error = candidate_error
            if (
                isinstance(candidate_stage, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", candidate_stage)
            ):
                error_stage = candidate_stage
        if terminal is None:
            status = _read_json(status_path)
            terminal = (
                "cancelled" if status.get("cancellation_requested") else "failed"
            )
            if terminal != "cancelled":
                error = (
                    "runner_container_disappeared"
                    if disappeared
                    else "runner_process_exited_before_terminal"
                )
                error_stage = (
                    "container_lifecycle"
                    if disappeared
                    else "bootstrap_or_controller"
                )
            _ensure_terminal_event(
                run_dir / "events.jsonl",
                status=terminal,
                error=error,
                exit_code=exit_code,
                error_stage=error_stage,
            )
        changes: dict[str, Any] = {
            "status": terminal,
            "phase": "terminal",
            "exit_code": exit_code,
            "container_id": None,
        }
        if terminal == "failed" and error is not None:
            changes["error"] = error
            changes["error_stage"] = error_stage
        _update_status(status_path, **changes)

    async def cancel(self, run_id: str, identity: RunIdentityRequest) -> bool:
        locator = self._read_run_locator(run_id)
        if (
            locator.get("user_id") != identity.user_id
            or locator.get("conversation_id") != identity.conversation_id
        ):
            raise HTTPException(404, "Run not found")
        admission = self._admission_dir(run_id)
        async with self._guard:
            admission_status = _read_json(admission / "status.json")
            phase = str(
                admission_status.get("phase")
                or admission_status.get("status")
                or ""
            )
            if str(admission_status.get("status") or "") in TERMINAL_STATUSES:
                return True
            _update_status(
                admission / "status.json", cancellation_requested=True
            )
            if phase == "preflight":
                # Revoke authority immediately. The isolated preflight worker
                # may still be stuck in an uninterruptible filesystem syscall,
                # but every post-attestation/Docker boundary rechecks this
                # durable local fence and can no longer launch a Turn.
                _ensure_terminal_event(
                    admission / "events.jsonl", status="cancelled", error=None
                )
                _update_status(
                    admission / "status.json",
                    status="cancelled",
                    phase="terminal",
                )
                return True

        run_dir = self._run_dir(identity.user_id, identity.conversation_id, run_id)
        status_path = run_dir / "status.json"
        task: asyncio.Task[None] | None = None
        cancelled_queued_task = False
        status = await asyncio.to_thread(_read_json, status_path)
        if (
            status.get("user_id") != identity.user_id
            or status.get("conversation_id") != identity.conversation_id
        ):
            raise HTTPException(404, "Run not found")
        terminal = await asyncio.to_thread(
            _terminal_status, run_dir / "events.jsonl"
        )
        if terminal is not None:
            await asyncio.to_thread(
                _update_status,
                status_path,
                status=terminal,
                phase="terminal",
                container_id=None,
            )
            return True
        await asyncio.to_thread(
            _update_status, status_path, cancellation_requested=True
        )
        task = self._tasks.get(run_id)
        phase = str(status.get("phase") or status.get("status") or "")
        if phase == "queued" and task is not None and not task.done():
            task.cancel()
            cancelled_queued_task = True
        if task is not None and cancelled_queued_task:
            await asyncio.gather(task, return_exceptions=True)
        container = None
        deadline = time.monotonic() + 15.0
        while True:
            status = await asyncio.to_thread(_read_json, status_path)
            terminal = await asyncio.to_thread(
                _terminal_status, run_dir / "events.jsonl"
            )
            if terminal is not None:
                break
            container_id = status.get("container_id")
            try:
                container = await asyncio.to_thread(
                    self.client.containers.get,
                    str(container_id or ("chatds-claude-" + run_id)),
                )
            except NotFound:
                container = None
            except DockerException as exc:
                raise HTTPException(
                    503, "Runner cancellation did not converge"
                ) from exc
            if container is not None:
                break
            task = self._tasks.get(run_id)
            if task is None or task.done():
                break
            if time.monotonic() >= deadline:
                raise HTTPException(503, "Runner cancellation did not converge")
            await asyncio.sleep(0.05)
        if container is not None:
            try:
                await asyncio.to_thread(container.stop, timeout=30)
                try:
                    await asyncio.to_thread(container.wait, timeout=40)
                except (NotFound, DockerException):
                    pass
            except DockerException as exc:
                raise HTTPException(503, "Runner cancellation did not converge") from exc
        # The in-container controller is the sole ledger writer while it is
        # alive.  Only synthesize cancellation after Docker confirms it has
        # stopped, avoiding concurrent append/sequence corruption on NFS.
        terminal = await asyncio.to_thread(
            _terminal_status, run_dir / "events.jsonl"
        )
        if terminal is None:
            await asyncio.to_thread(
                _ensure_terminal_event,
                run_dir / "events.jsonl",
                status="cancelled",
                error=None,
            )
            terminal = "cancelled"
        await asyncio.to_thread(
            _update_status,
            status_path,
            status=terminal,
            phase="terminal",
            container_id=None,
        )
        _update_status(
            admission / "status.json", status=terminal, phase="terminal"
        )
        return True

    async def decide_approval(
        self,
        run_id: str,
        request_id: str,
        decision: ApprovalDecisionRequest,
    ) -> dict[str, Any]:
        """Commit one decision to the active Turn's controller mailbox."""

        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id) is None:
            raise HTTPException(400, "Invalid approval request id")
        locator = self._read_run_locator(run_id)
        if (
            locator.get("user_id") != decision.user_id
            or locator.get("conversation_id") != decision.conversation_id
        ):
            raise HTTPException(404, "Run not found")
        run_dir = self._run_dir(
            decision.user_id, decision.conversation_id, run_id
        )
        request = _read_json(run_dir / "request.json")
        if _terminal_status(run_dir / "events.jsonl") is not None:
            raise HTTPException(409, "Run is no longer active")
        native_request: dict[str, Any] | None = None
        try:
            with (run_dir / "events.jsonl").open("r", encoding="utf-8") as handle:
                for line in handle:
                    envelope = json.loads(line)
                    if int(envelope.get("seq") or 0) != decision.request_seq:
                        continue
                    event = envelope.get("event")
                    inner = event.get("request") if isinstance(event, dict) else None
                    if (
                        isinstance(event, dict)
                        and event.get("type") == "control_request"
                        and event.get("request_id") == request_id
                        and isinstance(inner, dict)
                        and inner.get("subtype") == "can_use_tool"
                    ):
                        native_request = event
                    break
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(409, "Approval request is not durable") from exc
        if native_request is None:
            raise HTTPException(409, "Approval request is stale or not durable")
        inner = native_request["request"]
        tool_name = str(inner.get("tool_name") or "")
        interaction_kind = native_user_interaction_kind(
            tool_name, inner.get("input")
        )
        if is_native_user_interaction(tool_name) and interaction_kind is None:
            raise HTTPException(409, "Native interaction request is invalid")
        permission_preset = str(request.get("permission_preset") or "")
        if (
            permission_preset != "workspace_write"
            and interaction_kind is None
        ):
            raise HTTPException(
                409,
                "This permission tier does not accept that interactive decision",
            )
        if decision.decision == "deny" and decision.answers is not None:
            raise HTTPException(400, "Denied interactions cannot include answers")
        try:
            updated_input = (
                build_native_updated_input(
                    tool_name=tool_name,
                    native_input=inner.get("input"),
                    answers=decision.answers,
                )
                if decision.decision == "allow"
                else None
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        mailbox = run_dir / "approvals"
        mailbox.mkdir(mode=0o700, exist_ok=True)
        response_path = mailbox / (
            hashlib.sha256(request_id.encode()).hexdigest() + ".json"
        )
        response = {
            "schema": "chatds.claude-approval.v1",
            "run_id": run_id,
            "request_id": request_id,
            "request_seq": decision.request_seq,
            "decision": decision.decision,
            "tool_name": (tool_name or "tool")[:512],
            **(
                {"updated_input": updated_input}
                if updated_input is not None
                else {}
            ),
        }
        if response_path.exists():
            existing = _read_json(response_path)
            if existing != response:
                raise HTTPException(409, "Approval request already has another decision")
            return {
                "accepted": True,
                "idempotent": True,
                "status": "allowed" if decision.decision == "allow" else "denied",
            }
        _atomic_json(response_path, response, mode=0o600)
        return {
            "accepted": True,
            "idempotent": False,
            "status": "allowed" if decision.decision == "allow" else "denied",
        }

    async def cleanup_session(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        state = self._session_state(user_id, conversation_id)
        queued_tasks: list[asyncio.Task[None]] = []
        async with self._guard:
            self._revoked_sessions.add((user_id, conversation_id))
            # Fence local pending writes before touching Session/NFS state. A
            # blocked preflight cannot acquire Docker authority after this.
            admission_run_ids: set[str] = set()
            for locator_path in self._index_root.glob("*.json"):
                try:
                    locator = self._read_run_locator(locator_path.stem)
                except HTTPException:
                    continue
                if (
                    locator.get("user_id") != user_id
                    or locator.get("conversation_id") != conversation_id
                    or locator.get("schema") != "chatds.claude-run-locator.v2"
                ):
                    continue
                run_id = locator["run_id"]
                admission_run_ids.add(run_id)
                admission = self._admission_dir(run_id)
                try:
                    admission_status = _read_json(admission / "status.json")
                    _update_status(
                        admission / "status.json", cancellation_requested=True
                    )
                    if str(admission_status.get("phase") or "") == "preflight":
                        _ensure_terminal_event(
                            admission / "events.jsonl",
                            status="cancelled",
                            error=None,
                        )
                        _update_status(
                            admission / "status.json",
                            status="cancelled",
                            phase="terminal",
                        )
                except HTTPException:
                    continue
        # Discover already-prepared Session runs after the local fence. NFS
        # enumeration and status RMW never execute on the HTTP event loop.
        state_run_ids = await asyncio.to_thread(_state_run_ids, state)
        for run_id in state_run_ids:
            status_path = state / "control" / "runs" / run_id / "status.json"
            try:
                status = await asyncio.to_thread(_read_json, status_path)
                await asyncio.to_thread(
                    _update_status,
                    status_path,
                    cancellation_requested=True,
                )
            except HTTPException:
                continue
            task = self._tasks.get(run_id)
            if (
                str(status.get("phase") or status.get("status") or "")
                == "queued"
                and task is not None
                and not task.done()
            ):
                task.cancel()
                queued_tasks.append(task)
        if queued_tasks:
            await asyncio.gather(*queued_tasks, return_exceptions=True)
        conversation_hash = hashlib.sha256(conversation_id.encode()).hexdigest()
        user_hash = hashlib.sha256(user_id.encode()).hexdigest()
        failures = []
        run_ids: set[str] = set(state_run_ids) | admission_run_ids
        containers_by_id: dict[str, Any] = {}
        filters = {"label": [
            "chatds.component=claude-runner",
            f"chatds.user_sha256={user_hash}",
            f"chatds.conversation_sha256={conversation_hash}",
        ]}
        # A Docker create can overlap the first label query. The Session is
        # already irrevocably fenced above, and each pending status carries a
        # cancellation marker; poll until every start/monitor task converges.
        deadline = time.monotonic() + 15.0
        while True:
            containers = await asyncio.to_thread(
                self.client.containers.list,
                all=True,
                filters=filters,
            )
            for container in containers:
                containers_by_id[str(container.id)] = container
                run_id = str((container.labels or {}).get("chatds.run_id") or "")
                if SAFE_ID.fullmatch(run_id):
                    run_ids.add(run_id)
                try:
                    await asyncio.to_thread(container.stop, timeout=30)
                except NotFound:
                    pass
                except DockerException:
                    failures.append(container.id[:12])
            active = [
                task
                for run_id in run_ids
                if (task := self._tasks.get(run_id)) is not None and not task.done()
            ]
            if not active and not containers:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        pending = [
            self._tasks[run_id]
            for run_id in run_ids
            if run_id in self._tasks and not self._tasks[run_id].done()
        ]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=60.0
                )
            except asyncio.TimeoutError:
                failures.extend(sorted(run_ids))
        # Remove any stopped container that was not owned by a live watcher.
        for container in containers_by_id.values():
            try:
                await asyncio.to_thread(container.remove, force=True)
            except NotFound:
                pass
            except DockerException:
                failures.append(container.id[:12])
        if failures:
            return {
                "success": False,
                "execution_revocation": {"success": False, "residual_count": len(failures)},
            }
        if await asyncio.to_thread(state.exists):
            await asyncio.to_thread(_remove_state_tree, state)
        for run_id in run_ids:
            try:
                self._locator_path(run_id).unlink()
            except FileNotFoundError:
                pass
            shutil.rmtree(self._admission_dir(run_id), ignore_errors=True)
        return {
            "success": True,
            "execution_revocation": {"success": True, "residual_count": 0},
        }

    def event_paths(self, run_id: str) -> tuple[Path, Path, Path]:
        locator = self._read_run_locator(run_id)
        run_dir = self._run_dir(
            locator["user_id"], locator["conversation_id"], run_id
        )
        # Admission events cover only failures/cancellation before Session/NFS
        # preflight completes. Native/supervisor execution events remain in the
        # Session ledger. Constructing these paths performs no NFS I/O.
        return (
            self._admission_dir(run_id) / "events.jsonl",
            self._admission_dir(run_id) / "status.json",
            run_dir / "events.jsonl",
        )

    def _provider(self, request: StartRunRequest) -> ProviderProfile:
        profile = self.settings.provider_profiles.get(request.provider_profile)
        if profile is None:
            raise HTTPException(400, "Provider profile is not Claude-compatible")
        if request.api_model not in profile.models:
            raise HTTPException(400, "Model is not allowed by the provider profile")
        if (
            profile.context_windows.get(request.api_model)
            != request.context_window_tokens
        ):
            raise HTTPException(
                400,
                "Model context window does not match its deployment profile",
            )
        if request.provider_base_url.rstrip("/") != profile.backend_base_url:
            raise HTTPException(400, "Provider endpoint does not match its deployment profile")
        if request.provider_protocol != profile.backend_protocol:
            raise HTTPException(400, "Provider protocol does not match its deployment profile")
        return profile

    def _validate_paths(self, request: StartRunRequest) -> tuple[Path, Path, Path]:
        session = self.settings.workspace_host_root / request.user_id / request.conversation_id
        expected_workspace = session / "workspace"
        workspace = _exact_real_path(request.workspace_path, expected_workspace)
        skill_view = _exact_real_path(
            request.skill_view_path,
            session / "runtime" / "claude" / "skill-views" / request.skill_view_sha256,
        )
        state = session / "runtime" / "claude" / "state"
        state.mkdir(parents=True, exist_ok=True, mode=0o711)
        os.chmod(state, 0o711)
        control = state / "control"
        control.mkdir(exist_ok=True, mode=0o700)
        os.chmod(control, 0o700)
        return workspace, skill_view, state

    def _session_state(self, user_id: str, conversation_id: str) -> Path:
        if not SAFE_ID.fullmatch(user_id) or not SAFE_ID.fullmatch(conversation_id):
            raise HTTPException(404, "Session not found")
        return self.settings.workspace_host_root / user_id / conversation_id / "runtime" / "claude" / "state"

    def _run_dir(self, user_id: str, conversation_id: str, run_id: str) -> Path:
        if not SAFE_ID.fullmatch(run_id):
            raise HTTPException(404, "Run not found")
        return self._session_state(user_id, conversation_id) / "control" / "runs" / run_id

    async def detach_for_shutdown(self) -> None:
        """Stop local watchers without changing Docker-owned run authority."""

        async with self._guard:
            self._draining = True
            tasks = tuple(self._tasks.values())
            for task in tasks:
                if not task.done():
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._preflight_executor.shutdown(wait=False, cancel_futures=True)


def _build_prompt(
    messages: list[dict[str, Any]],
    *,
    resume: bool,
) -> str:
    if resume:
        for message in reversed(messages):
            if message.get("role") == "user":
                return _message_text(message.get("content"))
        raise HTTPException(400, "A resumed Claude Turn requires a user message")
    transcript = [
        message for message in messages
        if str(message.get("role") or "user").lower() != "system"
    ]
    if (
        len(transcript) == 1
        and str(transcript[0].get("role") or "user").lower() == "user"
    ):
        prompt = _message_text(transcript[0].get("content"))
        if prompt.strip():
            return prompt
        raise HTTPException(400, "A Claude Turn requires a non-empty user message")
    rendered = []
    for message in transcript:
        role = str(message.get("role") or "user").upper()
        rendered.append(f"<{role}>\n{_message_text(message.get('content'))}\n</{role}>")
    prompt = "\n\n".join(rendered)
    if not prompt.strip():
        raise HTTPException(400, "A Claude Turn requires a non-empty user message")
    return prompt


def _validate_native_skill_manifest(manifest: dict[str, Any]) -> None:
    """Verify native discovery inputs without injecting a synthetic command."""

    plugin_name = manifest.get("plugin_name")
    entrypoint_name = manifest.get("entrypoint_skill_name")
    selected = manifest.get("selected_primary_skill_names")
    if (
        plugin_name != EXPECTED_SKILL_PLUGIN_NAME
        or entrypoint_name is not None
        or not isinstance(selected, list)
        or any(
            not isinstance(name, str)
            or not name
            for name in selected
        )
        or selected != list(dict.fromkeys(selected))
    ):
        raise RuntimeError("native_skill_manifest_invalid")
    skills = manifest.get("skills")
    if not isinstance(skills, list):
        raise RuntimeError("native_skill_manifest_invalid")
    available = {
        str(row.get("name") or "")
        for row in skills
        if isinstance(row, dict)
    }
    if any(name not in available for name in selected):
        raise RuntimeError("native_skill_manifest_invalid")


def _fresh_session_skill_binding(
    manifest: dict[str, Any],
    *,
    resume: bool,
) -> dict[str, str] | None:
    """Bind one explicitly attached primary through Claude's native parser.

    Session-scoped Skills are uploaded or linked to this Conversation.  A
    single primary therefore has an unambiguous fresh-Session entrypoint.
    User-level Skills remain ambient, multiple Session primaries remain a
    model-owned routing choice, and resume relies on Claude's durable invoked
    Skill state instead of replaying an old command on every Turn.
    """

    if resume:
        return None
    selected = manifest.get("selected_primary_skill_names")
    skills = manifest.get("skills")
    if not isinstance(selected, list) or not isinstance(skills, list):
        raise RuntimeError("native_skill_manifest_invalid")
    selected_names = set(selected)
    candidates = []
    for row in skills:
        if not isinstance(row, dict):
            raise RuntimeError("native_skill_manifest_invalid")
        name = str(row.get("name") or "")
        if (
            name in selected_names
            and row.get("scope") == "session"
            and row.get("bundle_role") != "supporting"
        ):
            candidates.append(name)
    if len(candidates) != 1:
        return None
    return {
        "skill_name": candidates[0],
        "source": "fresh_session_primary",
    }


def _compile_turn_workflow_contract(
    *,
    manifest: dict[str, Any],
    user_turn_text: str,
    bound_skill_name: str | None,
) -> dict[str, Any] | None:
    return compile_native_workflow_contract(
        manifest=manifest,
        user_turn_text=user_turn_text,
        bound_skill_name=bound_skill_name,
    )


def _recover_start_request(path: Path) -> StartRunRequest:
    """Rebuild only the non-secret execution identity from a durable request.

    Prompt and egress policy were already compiled into ``request.json``. The
    reconstructed Pydantic request is used solely for path/provider identity,
    labels, and candidate checkpoint metadata; it never recompiles authority.
    """

    value = _read_json(path)
    if value.get("schema") != "chatds.claude-run.v1":
        raise ValueError("Durable Claude request schema is invalid")
    return StartRunRequest.model_validate({
        "run_id": value.get("run_id"),
        "root_run_id": value.get("root_run_id"),
        "user_id": value.get("user_id"),
        "conversation_id": value.get("conversation_id"),
        "model_id": value.get("model_id"),
        "api_model": value.get("api_model"),
        "provider_profile": value.get("provider_profile"),
        "provider_base_url": value.get("provider_backend_base_url"),
        "provider_protocol": value.get("provider_protocol") or "anthropic",
        "messages": [],
        "input_attachments": value.get("input_attachments") or [],
        "max_output_tokens": value.get("max_output_tokens"),
        "context_window_tokens": value.get("context_window_tokens"),
        "workspace_path": value.get("workspace_path"),
        "skill_view_path": value.get("skill_view_path"),
        "skill_view_sha256": value.get("skill_view_sha256"),
        "native_session_id": value.get("native_session_id"),
        "resume_from_native_session_id": value.get(
            "resume_from_native_session_id"
        ),
        "source": value.get("source") or "chat",
        "user_turn_text": "",
        "permission_preset": value.get("permission_preset") or "workspace_write",
    })


def _native_transcript_exists(state: Path, native_session_id: str) -> bool:
    home = state / "home" / ".claude" / "projects"
    if not home.is_dir() or home.is_symlink():
        return False
    matches = list(home.glob(f"*/{native_session_id}.jsonl"))
    return len(matches) == 1 and matches[0].is_file() and not matches[0].is_symlink()


def _message_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                pieces.append(str(item.get("text") or ""))
            elif isinstance(item, dict) and item.get("type") == "image_file":
                receipt = item.get("image_file")
                if not isinstance(receipt, dict):
                    raise RuntimeError("input_attachment_receipt_invalid")
                relative = str(receipt.get("path") or "")
                if INPUT_ATTACHMENT_PATH.fullmatch(relative) is None:
                    raise RuntimeError("input_attachment_path_invalid")
                # The receipt is verified separately and the Runner lowers the
                # corresponding bytes into Claude Code's native top-level image
                # content block.  Do not add controller advice or a duplicate
                # textual attachment representation to the user's prompt.
            elif isinstance(item, dict) and item.get("type") == "image_url":
                raise RuntimeError("input_attachment_transport_unlowered")
        return "\n".join(pieces)
    return str(value or "")


def _prepare_worker_tree(root: Path, worker_gid: int) -> None:
    count = 0
    for walk_root, dirs, files in os.walk(root, followlinks=False):
        current = Path(walk_root)
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("workspace_contains_unsafe_directory")
        os.chown(current, -1, worker_gid, follow_symlinks=False)
        os.chmod(current, stat.S_IMODE(info.st_mode) | 0o070, follow_symlinks=False)
        for name in [*dirs, *files]:
            count += 1
            if count > MAX_WORKSPACE_ENTRIES:
                raise RuntimeError("workspace_entry_limit_exceeded")
            path = current / name
            item = os.lstat(path)
            if stat.S_ISLNK(item.st_mode):
                continue
            if stat.S_ISDIR(item.st_mode):
                continue
            if not stat.S_ISREG(item.st_mode):
                raise RuntimeError("workspace_contains_special_file")
            os.chown(path, -1, worker_gid, follow_symlinks=False)
            os.chmod(path, stat.S_IMODE(item.st_mode) | 0o060, follow_symlinks=False)


@contextmanager
def _prepared_worker_workspace(root: Path, worker_gid: int):
    """Prepare permissions atomically, then let the Turn own the long lease.

    The child controller acquires the same local lock volume before starting
    Claude and holds it through terminal fsync.  Keeping the Supervisor's
    flock for the whole Turn would make that durable lease deadlock; releasing
    after preparation is safe because no model-controlled process exists yet.
    """

    with workspace_mutation_guard(root, timeout_seconds=60.0):
        _prepare_worker_tree(root, worker_gid)
    yield


def _seccomp_security_option() -> str:
    try:
        value = json.loads(SECCOMP_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("runner_seccomp_profile_unavailable") from exc
    if not isinstance(value, dict) or value.get("defaultAction") is None:
        raise RuntimeError("runner_seccomp_profile_invalid")
    return "seccomp=" + json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _runner_security_options(mode: str) -> list[str]:
    options = [_seccomp_security_option()]
    if mode == "seccomp_no_new_privileges":
        options.insert(0, "no-new-privileges:true")
    elif mode != "seccomp_stripped_setid":
        raise RuntimeError("runner_security_mode_invalid")
    return options


def _validate_runner_image_security(image, mode: str) -> None:
    labels = image.labels if isinstance(getattr(image, "labels", None), dict) else {}
    if labels.get(EGRESS_POLICY_LABEL) != EXPECTED_EGRESS_POLICY_RUNTIME:
        raise RuntimeError("runner_image_egress_policy_attestation_missing")
    if labels.get(RUNNER_RUNTIME_LABEL) != EXPECTED_RUNNER_RUNTIME:
        raise RuntimeError("runner_image_runtime_attestation_missing")
    if mode == "seccomp_stripped_setid" and labels.get(SETID_STRIPPED_LABEL) != "true":
        raise RuntimeError("runner_image_setid_attestation_missing")


def _validate_runner_image_self_test_output(payload: object) -> None:
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > 64 * 1024:
        raise RuntimeError("runner_image_self_test_invalid")
    try:
        rows = [
            json.loads(line)
            for line in bytes(payload).decode("utf-8").splitlines()
            if line.strip()
        ]
    except (UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeError("runner_image_self_test_invalid") from exc
    if rows != [{
        "schema": RUNNER_IMAGE_SELF_TEST_SCHEMA,
        "status": "ok",
        "mcp_entrypoints": 4,
        "compatibility_entrypoints": 4,
    }]:
        raise RuntimeError("runner_image_self_test_failed")


def _run_runner_image_self_test(client, image_name: str) -> None:
    """Run the immutable image through its unmodified production ENTRYPOINT."""

    try:
        payload = client.containers.run(
            image_name,
            command=[RUNNER_IMAGE_SELF_TEST_ARGUMENT],
            detach=False,
            remove=True,
            network_mode="none",
            read_only=True,
            user="0:0",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=64,
            mem_limit="512m",
            nano_cpus=1_000_000_000,
            tmpfs={
                "/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777",
            },
            stdout=True,
            stderr=True,
        )
    except DockerException as exc:
        raise RuntimeError("runner_image_self_test_failed") from exc
    _validate_runner_image_self_test_output(payload)


def _exact_real_path(value: str, expected: Path) -> Path:
    supplied = Path(value)
    try:
        if supplied != expected or supplied.resolve(strict=True) != expected.resolve(strict=True):
            raise HTTPException(400, "Runner path does not match the Session boundary")
        info = os.lstat(supplied)
    except OSError as exc:
        raise HTTPException(400, "Runner path is unavailable") from exc
    if supplied.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise HTTPException(400, "Runner path is unsafe")
    return supplied


def _scope_digest(*parts: str) -> str:
    digest = hashlib.sha256(b"chatds.claude-egress.v1\0")
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: object, *, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        _write_all(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _atomic_create_json(path: Path, value: object, *, mode: int) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    complete = False
    try:
        _write_all(fd, encoded)
        os.fsync(fd)
        complete = True
    finally:
        os.close(fd)
        if not complete:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("short durable write")
        written += count


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(404, "Run state is unavailable") from exc
    return value if isinstance(value, dict) else {}


def _update_status(path: Path, **changes: Any) -> None:
    # Cancellation and Docker-create completion run on different threads. A
    # process-local RMW lock prevents either update from erasing the other's
    # durable fields; atomic rename alone only protects file integrity.
    with _STATUS_UPDATE_LOCK:
        current = _read_json(path)
        current.update(changes)
        current["updated_at_unix_ms"] = int(time.time() * 1000)
        _atomic_json(path, current, mode=0o600)


def _terminal_event(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            envelope = json.loads(line)
            event = envelope.get("event") if isinstance(envelope, dict) else None
            if isinstance(event, dict) and event.get("type") == "chatds.supervisor.terminal":
                status = str(event.get("status") or "")
                if status in TERMINAL_STATUSES:
                    return dict(event)
    except (OSError, ValueError, TypeError):
        return None
    return None


def _terminal_status(path: Path) -> str | None:
    event = _terminal_event(path)
    return str(event["status"]) if event is not None else None


def _ensure_terminal_event(
    path: Path,
    *,
    status: str,
    error: str | None,
    exit_code: int | None = None,
    error_stage: str | None = None,
) -> None:
    with _STATUS_UPDATE_LOCK:
        if _terminal_status(path) is not None:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        seq = 1
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                if lines:
                    seq = int(json.loads(lines[-1]).get("seq") or 0) + 1
            except (OSError, ValueError, TypeError):
                seq = 1
        event: dict[str, Any] = {
            "type": "chatds.supervisor.terminal",
            "status": status,
            "error": error,
        }
        if exit_code is not None:
            event["exit_code"] = exit_code
        if error_stage is not None:
            event["error_stage"] = error_stage
        if error is not None:
            event["error_code"] = error
        envelope = {
            "seq": seq,
            "received_at_unix_ms": int(time.time() * 1000),
            "channel": "supervisor",
            "event": event,
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            _write_all(fd, json.dumps(
                envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)


def _remove_state_tree(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("state_tree_is_symlink")
    for walk_root, dirs, files in os.walk(path, followlinks=False):
        os.chmod(walk_root, 0o700, follow_symlinks=False)
        for name in files:
            item = Path(walk_root) / name
            if not item.is_symlink():
                os.chmod(item, 0o600, follow_symlinks=False)
    shutil.rmtree(path)


def _state_run_ids(state: Path) -> set[str]:
    root = state / "control" / "runs"
    if not root.is_dir():
        return set()
    return {
        child.name
        for child in root.iterdir()
        if child.is_dir()
        and not child.is_symlink()
        and SAFE_ID.fullmatch(child.name)
    }


async def _event_stream(
    sources: tuple[Path, Path, Path], after: int
) -> AsyncIterator[str]:
    admission_events, admission_status, native_events = sources
    cursor = after
    heartbeat = time.monotonic()
    byte_offsets = {admission_events: 0, native_events: 0}
    pending = {admission_events: bytearray(), native_events: bytearray()}
    reads: dict[Path, asyncio.Task[bytes]] = {}
    try:
        while True:
            # Never probe the Session/NFS ledger until preflight has completed.
            # A local cancellation/failure therefore remains immediately
            # observable even if the immutable Skill tree is hard-stalled.
            try:
                phase = str(_read_json(admission_status).get("phase") or "")
            except HTTPException:
                phase = "preflight"
            active_paths = [admission_events]
            if phase != "preflight":
                active_paths.append(native_events)
            for path in active_paths:
                task = reads.get(path)
                if task is None:
                    reads[path] = asyncio.create_task(
                        asyncio.to_thread(
                            _read_ledger_chunk, path, byte_offsets[path]
                        )
                    )
                    continue
                if not task.done():
                    continue
                reads.pop(path, None)
                try:
                    chunk = task.result()
                except OSError:
                    chunk = b""
                if chunk:
                    byte_offsets[path] += len(chunk)
                    pending[path].extend(chunk)
                if len(pending[path]) > 72 * 1024 * 1024:
                    raise RuntimeError(
                        "Claude Runner event line exceeded its durable bound"
                    )
                while b"\n" in pending[path]:
                    raw_line, _, remainder = pending[path].partition(b"\n")
                    pending[path] = bytearray(remainder)
                    try:
                        envelope = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                    seq = envelope.get("seq") if isinstance(envelope, dict) else None
                    if not isinstance(seq, int) or seq <= cursor:
                        continue
                    cursor = seq
                    yield (
                        "data: "
                        + json.dumps(
                            envelope,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n\n"
                    )
                    event = envelope.get("event")
                    if (
                        isinstance(event, dict)
                        and event.get("type") == "chatds.supervisor.terminal"
                    ):
                        yield "data: [DONE]\n\n"
                        return
            if time.monotonic() - heartbeat >= 15:
                heartbeat = time.monotonic()
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.25)
    finally:
        for task in reads.values():
            task.cancel()


def _read_ledger_chunk(path: Path, offset: int) -> bytes:
    with path.open("rb") as stream:
        stream.seek(offset)
        return stream.read(1024 * 1024)


settings: RunnerSettings | None = None
manager: RunManager | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global settings, manager
    settings = load_settings()
    if docker is None:
        raise RuntimeError("Docker SDK is unavailable")
    client = docker.from_env()
    client.ping()
    image = client.images.get(settings.runner_image)
    _validate_runner_image_security(image, settings.security_mode)
    client.volumes.get(settings.egress_proxy_volume)
    client.volumes.get(settings.workspace_lock_volume)
    await asyncio.to_thread(
        _run_runner_image_self_test,
        client,
        settings.runner_image,
    )
    manager = RunManager(settings, client)
    reconciliation = await manager.reconcile_existing_containers()
    if any(reconciliation.values()):
        logger.warning("Claude Runner startup reconciliation: %s", reconciliation)
    yield
    # Runs are Docker-owned and continue across an HTTP Supervisor restart.
    # Detaching watchers must never project a user cancellation or remove a
    # live Turn container. OS process teardown closes the Docker transport;
    # the replacement Supervisor adopts labelled containers before serving.
    await manager.detach_for_shutdown()


app = FastAPI(title="ChatDS Claude Runner Supervisor", lifespan=lifespan)


def _require_internal_token(x_internal_token: str = Header(default="")) -> None:
    if settings is None or not _constant_time_equal(x_internal_token, settings.internal_token):
        raise HTTPException(401, "Unauthorized")


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@app.get("/health")
async def health(_auth=Depends(_require_internal_token)):
    if manager is None:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "code": "not_ready"},
        )
    try:
        await asyncio.to_thread(manager.client.ping)
        image = await asyncio.to_thread(manager.client.images.get, manager.settings.runner_image)
        _validate_runner_image_security(image, manager.settings.security_mode)
        await asyncio.to_thread(
            manager.client.volumes.get, manager.settings.workspace_lock_volume
        )
    except (DockerException, ImageNotFound):
        return JSONResponse(
            status_code=503,
            content={"status": "error", "code": "docker_or_image_unavailable"},
        )
    return {
        "status": "ok",
        "claude_version": image.labels.get("org.opencontainers.image.version", "unknown"),
        "network_policy": (
            "network-none+signed-exact-and-public-read-egress-v3"
        ),
        "security_mode": manager.settings.security_mode,
    }


@app.post("/v1/runs")
async def start_run(payload: StartRunRequest, _auth=Depends(_require_internal_token)):
    assert manager is not None
    return await manager.start(payload)


@app.get("/v1/runs/{run_id}/events")
async def run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    _auth=Depends(_require_internal_token),
):
    assert manager is not None
    paths = manager.event_paths(run_id)
    return StreamingResponse(
        _event_stream(paths, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    payload: RunIdentityRequest,
    _auth=Depends(_require_internal_token),
):
    assert manager is not None
    return {"success": await manager.cancel(run_id, payload)}


@app.post("/v1/runs/{run_id}/approvals/{request_id}")
async def decide_approval(
    run_id: str,
    request_id: str,
    payload: ApprovalDecisionRequest,
    _auth=Depends(_require_internal_token),
):
    assert manager is not None
    return await manager.decide_approval(run_id, request_id, payload)


@app.post("/v1/sessions/{conversation_id}/cleanup")
async def cleanup_session(
    conversation_id: str,
    payload: SessionIdentityRequest,
    _auth=Depends(_require_internal_token),
):
    assert manager is not None
    return await manager.cleanup_session(payload.user_id, conversation_id)
