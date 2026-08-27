"""Trusted Docker supervisor for isolated upstream DeepSeek Harness Turns."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from claude_runner.policy import compile_turn_egress_policy, verify_skill_view
from workspace_lock import workspace_mutation_guard

from .config import ProviderProfile, Settings, load_settings, state_volume_host_root
from .control_decisions import (
    answerable_control_request,
    append_control_decision,
    existing_control_decision,
    lower_native_question_answer,
)
from .event_stream import read_event_tail
from .native_session import (
    NativeSessionInputError,
    bind_native_primary_skill,
    native_turn_prompts,
)
from .native_workflow import compile_deepseek_workflow_projection
from .terminal_receipts import append_terminal, terminal_receipt, terminal_status
from native_security.workflow_contract import compile_turn_workflow_contract
from native_security.run_control import (
    RunControlError,
    build_run_control,
    enqueue_run_control,
    next_run_control_seq,
    read_run_control,
    read_run_control_receipt,
    receipt_path as run_control_receipt_path,
    request_path as run_control_request_path,
    write_run_control_receipt,
)


SAFE_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_ENTRIES = 200_000


async def _wait_for_container_exit(
    container,
    max_run_seconds: int | None,
) -> dict[str, Any]:
    wait = asyncio.to_thread(container.wait)
    if max_run_seconds is None:
        return await wait
    return await asyncio.wait_for(wait, timeout=max_run_seconds)


class StartRunRequest(BaseModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    root_run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    conversation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    native_session_id: str = Field(pattern=r"^chatds-[0-9a-f]{32}$")
    model_id: str = Field(min_length=1, max_length=128)
    api_model: str = Field(min_length=1, max_length=128)
    provider_profile: str = Field(min_length=1, max_length=64)
    provider_base_url: str = Field(min_length=8, max_length=2048)
    provider_protocol: Literal["openai"]
    messages: list[dict[str, Any]] = Field(max_length=4096)
    max_output_tokens: int = Field(ge=1, le=262_144)
    context_window_tokens: int = Field(ge=32_000, le=4_000_000)
    workspace_path: str = Field(min_length=1, max_length=4096)
    skill_view_path: str = Field(min_length=1, max_length=4096)
    skill_view_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str = Field(default="chat", max_length=24)
    user_turn_text: str = Field(default="", max_length=2_000_000)
    permission_preset: Literal[
        "read_only", "workspace_write", "session_full"
    ] = "workspace_write"

    @model_validator(mode="after")
    def bounded(self):
        payload = json.dumps(
            self.model_dump(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("DeepSeek Harness run request is too large")
        return self


class RunIdentity(BaseModel):
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    conversation_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class RunControlRequest(RunIdentity):
    model_config = {"extra": "forbid"}

    control_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    action: Literal["interrupt", "followup", "steer"]
    text: str | None = Field(default=None, max_length=2_000_000)

    @model_validator(mode="after")
    def exact_control(self):
        if self.action == "interrupt":
            if self.text is not None:
                raise ValueError("Interrupt controls cannot contain text")
        elif self.text is None or not self.text.strip() or "\x00" in self.text:
            raise ValueError("Native message control text is invalid")
        return self


class ApprovalDecisionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    conversation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    decision: Literal["allow", "deny"]
    request_seq: int = Field(ge=1)
    answers: dict[str, str] | None = None

    @model_validator(mode="after")
    def bounded_answers(self):
        if self.answers is not None and (
            len(self.answers) != 1
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


class SessionIdentity(BaseModel):
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def _scope(*parts: str) -> str:
    digest = hashlib.sha256(b"chatds.deepseek-egress.v1\0")
    for part in parts:
        data = part.encode()
        digest.update(len(data).to_bytes(4, "big"))
        digest.update(data)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode() + b"\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            offset += os.write(fd, view[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(404, "DeepSeek Harness run state is unavailable") from exc
    if not isinstance(value, dict):
        raise HTTPException(404, "DeepSeek Harness run state is unavailable")
    return value


def _terminal(path: Path) -> str | None:
    return terminal_status(path)


def _exact_directory(value: str, expected: Path) -> Path:
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


def _prepare_workspace(root: Path, gid: int) -> None:
    count = 0
    with workspace_mutation_guard(root, timeout_seconds=60):
        for walk_root, dirs, files in os.walk(root, followlinks=False):
            current = Path(walk_root)
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("workspace_contains_unsafe_directory")
            os.chown(current, -1, gid, follow_symlinks=False)
            os.chmod(current, stat.S_IMODE(info.st_mode) | 0o070)
            for name in [*dirs, *files]:
                count += 1
                if count > MAX_WORKSPACE_ENTRIES:
                    raise RuntimeError("workspace_entry_limit_exceeded")
                path = current / name
                item = os.lstat(path)
                if stat.S_ISLNK(item.st_mode) or stat.S_ISDIR(item.st_mode):
                    continue
                if not stat.S_ISREG(item.st_mode):
                    raise RuntimeError("workspace_contains_special_file")
                os.chown(path, -1, gid, follow_symlinks=False)
                os.chmod(path, stat.S_IMODE(item.st_mode) | 0o060)


class Manager:
    def __init__(self, settings: Settings, client, *, state_host_root: Path) -> None:
        self.settings = settings
        self.client = client
        self.state_host_root = state_host_root
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_runs)
        self.guard = asyncio.Lock()
        (settings.state_root / "run-index").mkdir(parents=True, exist_ok=True, mode=0o700)

    def _control(self, user: str, conversation: str, run: str) -> Path:
        return self.settings.state_root / "users" / user / conversation / "runs" / run

    def _control_host(self, user: str, conversation: str, run: str) -> Path:
        return self.state_host_root / "users" / user / conversation / "runs" / run

    def _locator(self, run: str) -> Path:
        if not SAFE_ID.fullmatch(run):
            raise HTTPException(404, "Run not found")
        return self.settings.state_root / "run-index" / f"{run}.json"

    def _locate(self, run: str) -> dict[str, str]:
        value = _read_json(self._locator(run))
        if value.get("run_id") != run:
            raise HTTPException(404, "Run not found")
        return {key: str(value[key]) for key in ("run_id", "user_id", "conversation_id")}

    def _profile(self, request: StartRunRequest) -> ProviderProfile:
        profile = self.settings.provider_profiles.get(request.provider_profile)
        if (
            profile is None
            or request.api_model not in profile.models
            or profile.context_windows.get(request.api_model) != request.context_window_tokens
            or profile.base_url != request.provider_base_url.rstrip("/")
        ):
            raise HTTPException(400, "Provider binding does not match deployment authority")
        return profile

    def _paths(self, request: StartRunRequest) -> tuple[Path, Path, Path]:
        session = self.settings.workspace_host_root / request.user_id / request.conversation_id
        workspace = _exact_directory(request.workspace_path, session / "workspace")
        skill = _exact_directory(
            request.skill_view_path,
            session / "runtime" / "claude" / "skill-views" / request.skill_view_sha256,
        )
        state = session / "runtime" / "deepseek-harness" / "state"
        state.mkdir(parents=True, exist_ok=True, mode=0o711)
        os.chmod(state, 0o711)
        return workspace, skill, state

    async def start(self, request: StartRunRequest) -> dict[str, Any]:
        profile = self._profile(request)
        request_value = request.model_dump(mode="json")
        request_sha = _digest(request_value)
        control = self._control(request.user_id, request.conversation_id, request.run_id)
        locator = self._locator(request.run_id)
        async with self.guard:
            if locator.exists():
                existing = _read_json(locator)
                if existing.get("request_sha256") != request_sha:
                    raise HTTPException(409, "Run id belongs to a different request")
                return {"accepted": True, "idempotent": True, "run_id": request.run_id}
            control.mkdir(parents=True, mode=0o700)
            _atomic_json(control / "admission.json", request_value)
            _atomic_json(control / "status.json", {
                "status": "queued", "phase": "preflight", "container_id": None,
                "created_at_unix_ms": int(time.time() * 1000),
            })
            (control / "events.jsonl").touch(mode=0o600, exist_ok=False)
            _atomic_json(locator, {
                "run_id": request.run_id,
                "user_id": request.user_id,
                "conversation_id": request.conversation_id,
                "request_sha256": request_sha,
            })
            task = asyncio.create_task(self._execute(request, profile))
            self.tasks[request.run_id] = task
            task.add_done_callback(lambda _task, run=request.run_id: self.tasks.pop(run, None))
        return {"accepted": True, "idempotent": False, "run_id": request.run_id}

    async def _execute(self, request: StartRunRequest, profile: ProviderProfile) -> None:
        control = self._control(request.user_id, request.conversation_id, request.run_id)
        status_path = control / "status.json"
        events = control / "events.jsonl"
        container = None
        stage = "path_validation"
        try:
            workspace, skill, state = await asyncio.to_thread(self._paths, request)
            stage = "skill_attestation"
            receipt = await asyncio.to_thread(verify_skill_view, skill, request.skill_view_sha256)
            # This deployment replaces only DSH's search provider with the
            # bounded SearXNG adapter. The upstream web tool itself remains a
            # native, always-present part of the engine tool graph.
            web_enabled = True
            stage = "egress_policy"
            policy = await asyncio.to_thread(
                compile_turn_egress_policy,
                skill_view_root=skill,
                skill_view_sha256=request.skill_view_sha256,
                verified_skill_view=receipt,
                user_turn_text=request.user_turn_text,
                provider_base_url=profile.base_url,
                provider_protocol="openai",
                configured_private_origins=self.settings.private_origin_allowlist,
                budget_scope_sha256=_scope(
                    "budget", request.user_id, request.conversation_id, request.root_run_id
                ),
                call_id_sha256=_scope("call", request.root_run_id, request.run_id),
                limits=dict(self.settings.egress_limits),
                provider_response_idle_timeout_seconds=(
                    profile.response_idle_timeout_seconds
                ),
                public_read_enabled=self.settings.public_read_egress_enabled,
            )
            if web_enabled and not any(
                rule.get("url_prefix", "").split("?", 1)[0]
                == self.settings.searxng_search_url
                for rule in policy.get("egress_rules", [])
            ):
                raise RuntimeError("searxng_capability_policy_missing")
            stage = "native_session_binding"
            try:
                initial_prompt, turn_prompt = native_turn_prompts(
                    request.messages,
                    request.user_turn_text,
                )
            except NativeSessionInputError as exc:
                raise HTTPException(400, str(exc)) from exc
            execution_request = request.model_dump(mode="json")
            stage = "workflow_compilation"
            selected_primary = receipt.manifest.get(
                "selected_primary_skill_names", []
            )
            if not isinstance(selected_primary, list):
                raise RuntimeError("native_skill_manifest_invalid")
            bound_skill_name = (
                selected_primary[0]
                if len(selected_primary) == 1
                and isinstance(selected_primary[0], str)
                else None
            )
            initial_prompt = bind_native_primary_skill(
                initial_prompt,
                bound_skill_name,
            )
            workflow_contract = compile_turn_workflow_contract(
                manifest=receipt.manifest,
                user_turn_text=request.user_turn_text,
                bound_skill_name=bound_skill_name,
            )
            workflow_projection = compile_deepseek_workflow_projection(
                manifest=receipt.manifest,
                workflow_contract=workflow_contract,
                user_turn_text=request.user_turn_text,
                native_session_id=request.native_session_id,
            )
            # The trusted admission record retains the browser transcript. The
            # isolated runner receives only the two native Session inputs and
            # never replays the whole transcript after persistence exists.
            execution_request.pop("messages", None)
            sanitized = {
                "schema": "chatds.deepseek-run.v1",
                **execution_request,
                "initial_prompt": initial_prompt,
                "turn_prompt": turn_prompt,
                "provider_base_url": profile.base_url,
                "provider_reasoning_wire_effort": (
                    profile.reasoning_wire_efforts[request.api_model]
                ),
                "searxng_search_url": self.settings.searxng_search_url,
                "web_search_enabled": web_enabled,
                "egress_policy": policy,
                "workflow_contract": workflow_contract,
                "workflow_projection": workflow_projection,
                "bound_skill_name": bound_skill_name,
                "artifact_contracts": receipt.manifest.get(
                    "artifact_contracts", []
                ),
            }
            stage = "execution_request_persist"
            _atomic_json(control / "request.json", sanitized)
            stage = "workspace_preparation"
            await asyncio.to_thread(_prepare_workspace, workspace, self.settings.worker_gid)
            home = state / "home"
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(home, self.settings.worker_uid, self.settings.worker_gid)
            stage = "runner_attestation"
            image = await asyncio.to_thread(self.client.images.get, self.settings.runner_image)
            _verify_runner_image(image)
            stage = "container_preparation"
            volumes = {
                str(workspace): {
                    "bind": "/workspace",
                    "mode": "ro" if request.permission_preset == "read_only" else "rw",
                },
                str(state): {"bind": "/state", "mode": "rw"},
                str(skill): {"bind": "/skill-view", "mode": "ro"},
                str(self._control_host(
                    request.user_id, request.conversation_id, request.run_id
                )): {"bind": "/run/chatds-control", "mode": "rw"},
                self.settings.egress_proxy_volume: {
                    "bind": "/run/chatds-skill-egress", "mode": "ro",
                },
                self.settings.workspace_lock_volume: {
                    "bind": "/run/chatds-workspace-lock-plane", "mode": "rw",
                },
            }
            status = _read_json(status_path)
            status.update({"status": "starting", "phase": "starting"})
            _atomic_json(status_path, status)
            async with self.semaphore:
                stage = "native_container_start"
                container = await asyncio.to_thread(
                    self.client.containers.run,
                    self.settings.runner_image,
                    name="chatds-deepseek-" + request.run_id,
                    detach=True,
                    network_mode="none",
                    read_only=True,
                    user="0:0",
                    group_add=["65530"],
                    cap_drop=["ALL"],
                    cap_add=["CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID"],
                    security_opt=["no-new-privileges:true", "seccomp=unconfined"],
                    pids_limit=1024,
                    mem_limit=os.environ.get("DEEPSEEK_HARNESS_RUNNER_MEMORY_LIMIT", "8g"),
                    nano_cpus=int(float(os.environ.get("DEEPSEEK_HARNESS_RUNNER_CPUS", "6")) * 1_000_000_000),
                    tmpfs={
                        "/tmp": "rw,noexec,nosuid,nodev,size=2g,mode=1777",
                        "/runtime": "rw,nosuid,nodev,size=128m,mode=0755",
                        "/dev/shm": "rw,nosuid,nodev,size=1g,mode=1777",
                    },
                    volumes=volumes,
                    environment={
                        "CHATDS_RUN_CONFIG": "/run/chatds-control/request.json",
                        "DEEPSEEK_HARNESS_PROVIDER_API_KEY": profile.api_key,
                        "SKILL_EGRESS_POLICY_TOKEN": os.environ["SKILL_EGRESS_POLICY_TOKEN"],
                        "DEEPSEEK_HARNESS_RUNNER_WORKER_UID": str(self.settings.worker_uid),
                        "DEEPSEEK_HARNESS_RUNNER_WORKER_GID": str(self.settings.worker_gid),
                    },
                    labels={
                        "chatds.component": "deepseek-harness-runner",
                        "chatds.run_id": request.run_id,
                        "chatds.user_sha256": hashlib.sha256(request.user_id.encode()).hexdigest(),
                        "chatds.conversation_sha256": hashlib.sha256(request.conversation_id.encode()).hexdigest(),
                    },
                )
                status = _read_json(status_path)
                status.update({"status": "running", "phase": "running", "container_id": container.id})
                _atomic_json(status_path, status)
                stage = "native_execution"
                try:
                    result = await _wait_for_container_exit(
                        container,
                        self.settings.max_run_seconds,
                    )
                    exit_code = int(result.get("StatusCode", 1))
                except asyncio.TimeoutError:
                    # Ask the trusted PID 1 supervisor to terminate the native
                    # worker and persist its own terminal event first.  The
                    # Docker supervisor is only the fallback terminal writer;
                    # writing before the container exits races the runner's
                    # controller-owned ledger and can create two terminals.
                    try:
                        await asyncio.to_thread(container.kill, signal="SIGUSR1")
                    except (DockerException, NotFound):
                        pass
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(container.wait),
                            timeout=30,
                        )
                    except asyncio.TimeoutError:
                        try:
                            await asyncio.to_thread(container.kill, signal="SIGKILL")
                            await asyncio.wait_for(
                                asyncio.to_thread(container.wait),
                                timeout=15,
                            )
                        except (asyncio.TimeoutError, DockerException, NotFound):
                            pass
                    exit_code = 124
                    if _terminal(events) is None:
                        append_terminal(
                            events,
                            "failed",
                            "run_hard_timeout",
                            error_stage="native_execution",
                        )
                if _terminal(events) is None:
                    failure_code = _container_failure_code(container, exit_code)
                    append_terminal(
                        events,
                        "failed",
                        failure_code if exit_code else "terminal_missing",
                        error_stage="terminal_reconciliation",
                    )
        except asyncio.CancelledError:
            if container is not None:
                try:
                    await asyncio.to_thread(container.stop, timeout=15)
                except (DockerException, NotFound):
                    pass
            append_terminal(events, "cancelled", None)
            raise
        except BaseException as exc:
            append_terminal(
                events,
                "failed",
                (str(exc) or type(exc).__name__)[:128],
                error_stage=stage,
            )
        finally:
            if container is not None:
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except (DockerException, NotFound):
                    pass
            status = _read_json(status_path)
            status.update({
                "status": _terminal(events) or "failed",
                "phase": "terminal",
                "container_id": None,
            })
            _atomic_json(status_path, status)

    async def cancel(self, run: str, identity: RunIdentity) -> bool:
        locator = self._locate(run)
        if locator["user_id"] != identity.user_id or locator["conversation_id"] != identity.conversation_id:
            raise HTTPException(404, "Run not found")
        task = self.tasks.get(run)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        control = self._control(identity.user_id, identity.conversation_id, run)
        append_terminal(control / "events.jsonl", "cancelled", None)
        return True

    async def control(
        self,
        run: str,
        payload: RunControlRequest,
    ) -> dict[str, Any]:
        """Deliver one control through the pinned DSH Agent Host API."""

        locator = self._locate(run)
        if (
            locator["user_id"] != payload.user_id
            or locator["conversation_id"] != payload.conversation_id
        ):
            raise HTTPException(404, "Run not found")
        control = self._control(payload.user_id, payload.conversation_id, run)
        controls = control / "controls"
        try:
            async with self.guard:
                status = await asyncio.to_thread(
                    _read_json, control / "status.json"
                )
                terminal = await asyncio.to_thread(
                    _terminal, control / "events.jsonl"
                )
                if terminal is not None or str(status.get("status") or "") not in {
                    "starting", "running"
                }:
                    raise HTTPException(409, "Run is no longer accepting native controls")
                request_path = run_control_request_path(
                    controls, payload.control_id
                )
                if request_path.exists():
                    request = await asyncio.to_thread(
                        read_run_control, request_path
                    )
                    if (
                        request.get("action") != payload.action
                        or request.get("text") != payload.text
                    ):
                        raise HTTPException(409, "Control id already has different content")
                    idempotent = True
                else:
                    seq = await asyncio.to_thread(
                        next_run_control_seq, controls
                    )
                    request = build_run_control(
                        control_id=payload.control_id,
                        seq=seq,
                        action=payload.action,
                        text=payload.text,
                    )
                    idempotent = await asyncio.to_thread(
                        enqueue_run_control, controls, request
                    )
        except RunControlError as exc:
            raise HTTPException(409, str(exc)) from exc

        receipt_path = run_control_receipt_path(
            controls, payload.control_id
        )
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if receipt_path.exists():
                try:
                    receipt = await asyncio.to_thread(
                        read_run_control_receipt,
                        receipt_path,
                        request=request,
                    )
                except RunControlError as exc:
                    raise HTTPException(409, str(exc)) from exc
                return {
                    "accepted": receipt["status"] == "delivered",
                    "idempotent": idempotent,
                    **receipt,
                }
            terminal = await asyncio.to_thread(
                _terminal, control / "events.jsonl"
            )
            if terminal is not None:
                try:
                    receipt = await asyncio.to_thread(
                        write_run_control_receipt,
                        controls,
                        request,
                        status="rejected",
                        code="run_terminal_before_delivery",
                    )
                except RunControlError as exc:
                    raise HTTPException(409, str(exc)) from exc
                return {
                    "accepted": False,
                    "idempotent": idempotent,
                    **receipt,
                }
            await asyncio.sleep(0.05)
        return {
            "accepted": False,
            "idempotent": idempotent,
            "schema": "chatds.native-run-control-pending.v1",
            "control_id": request["control_id"],
            "seq": request["seq"],
            "action": request["action"],
            "status": "pending",
            "code": "delivery_pending",
        }

    async def decide_approval(self, run: str, request_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id) is None:
            raise HTTPException(400, "Invalid approval request id")
        locator = self._locate(run)
        if locator["user_id"] != payload.user_id or locator["conversation_id"] != payload.conversation_id:
            raise HTTPException(404, "Run not found")
        control = self._control(payload.user_id, payload.conversation_id, run)
        async with self.guard:
            events = control / "events.jsonl"
            if _terminal(events) is not None:
                raise HTTPException(409, "Run is no longer active")
            request_receipt = answerable_control_request(events, request_id)
            if (
                request_receipt is None
                or request_receipt[0] != payload.request_seq
            ):
                raise HTTPException(409, "Approval request is stale or not durable")
            native_request = request_receipt[1]
            native_type = str(native_request.get("type") or "")
            native_data = native_request.get("data")
            native_data = native_data if isinstance(native_data, dict) else {}
            if native_type == "chatds/question/requested":
                plan_review = native_data.get("intent_kind") == "plan-review"
                if plan_review:
                    if payload.answers is not None:
                        raise HTTPException(
                            400,
                            "Plan review accepts a binary decision, not answers",
                        )
                    selected = None
                    custom = None
                else:
                    question = str(native_data.get("question") or "")
                    if (
                        payload.decision == "allow"
                        and (
                            payload.answers is None
                            or set(payload.answers) != {question}
                        )
                    ):
                        raise HTTPException(400, "Question answer does not match the durable request")
                    if payload.decision == "deny" and payload.answers is not None:
                        raise HTTPException(400, "Denied questions cannot include answers")
                    answer = (
                        payload.answers[question]
                        if payload.answers is not None
                        else ""
                    )
                    try:
                        selected, custom = lower_native_question_answer(
                            native_data,
                            answer,
                        )
                    except ValueError as exc:
                        raise HTTPException(
                            409, "Native question request is invalid"
                        ) from exc
            else:
                if payload.answers is not None:
                    raise HTTPException(400, "Approval requests do not accept answers")
                selected = None
                custom = None
            mailbox = control / "control-decisions.jsonl"
            mailbox.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            row = {
                "request_id": request_id,
                "request_seq": payload.request_seq,
                "decision": payload.decision,
                **({"selected": selected} if selected is not None else {}),
                **({"custom": custom} if custom is not None else {}),
            }
            existing = existing_control_decision(mailbox, request_id)
            if existing is not None:
                if existing != row:
                    raise HTTPException(409, "Approval request already has another decision")
                return {
                    "accepted": True,
                    "idempotent": True,
                    "request_id": request_id,
                    "decision": payload.decision,
                }
            append_control_decision(mailbox, row)
        return {
            "accepted": True,
            "idempotent": False,
            "request_id": request_id,
            "decision": payload.decision,
        }

    async def cleanup(self, user: str, conversation: str) -> dict[str, Any]:
        if not SAFE_ID.fullmatch(user) or not SAFE_ID.fullmatch(conversation):
            raise HTTPException(404, "Session not found")
        prefix = self.settings.state_root / "users" / user / conversation
        active = [
            run for run, task in self.tasks.items()
            if not task.done() and self._locate(run)["conversation_id"] == conversation
            and self._locate(run)["user_id"] == user
        ]
        if active:
            raise HTTPException(409, "Session still owns an active DeepSeek Harness Turn")
        run_ids = []
        runs = prefix / "runs"
        if runs.is_dir():
            run_ids = [path.name for path in runs.iterdir() if SAFE_ID.fullmatch(path.name)]
        for run in run_ids:
            self._locator(run).unlink(missing_ok=True)
        if prefix.exists():
            shutil.rmtree(prefix)
        session_state = (
            self.settings.workspace_host_root / user / conversation
            / "runtime" / "deepseek-harness" / "state"
        )
        if session_state.exists():
            shutil.rmtree(session_state)
        return {
            "success": True,
            "execution_revocation": {"success": True},
            "removed_runs": len(run_ids),
        }

    def event_path(self, run: str) -> Path:
        locator = self._locate(run)
        return self._control(locator["user_id"], locator["conversation_id"], run) / "events.jsonl"

    def terminal(self, run: str, identity: RunIdentity) -> dict[str, Any]:
        locator = self._locate(run)
        if (
            locator["user_id"] != identity.user_id
            or locator["conversation_id"] != identity.conversation_id
        ):
            raise HTTPException(404, "Run not found")
        envelope, last_sequence = terminal_receipt(
            self._control(identity.user_id, identity.conversation_id, run)
            / "events.jsonl"
        )
        return {"terminal": envelope, "last_seq": last_sequence}


def _read_event_tail(path: Path, position: int) -> tuple[int, tuple[bytes, ...]]:
    return read_event_tail(path, position)


async def _stream(path: Path, after: int) -> AsyncIterator[str]:
    cursor = after
    position = 0
    heartbeat = time.monotonic()
    while True:
        if path.exists():
            position, lines = await asyncio.to_thread(
                _read_event_tail, path, position
            )
            for line in lines:
                try:
                    envelope = json.loads(line)
                except (ValueError, TypeError):
                    continue
                seq = envelope.get("seq") if isinstance(envelope, dict) else None
                if not isinstance(seq, int) or seq <= cursor:
                    continue
                cursor = seq
                yield "data: " + json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n\n"
                if envelope.get("event", {}).get("type") == "chatds.supervisor.terminal":
                    yield "data: [DONE]\n\n"
                    return
        if time.monotonic() - heartbeat >= 15:
            heartbeat = time.monotonic()
            yield ": heartbeat\n\n"
        await asyncio.sleep(0.25)


settings: Settings | None = None
manager: Manager | None = None


def _verify_runner_image(image) -> None:
    labels = image.labels or {}
    expected = {
        "org.opencontainers.image.version": "0.1.0-rc.5",
        "org.opencontainers.image.revision": (
            "47f943859bef60e4160492346772ded9b24f765a"
        ),
        "org.opencontainers.image.chatds.workspace-scope": "one-session",
        "org.opencontainers.image.chatds.native-session-driver": "v1",
        "org.opencontainers.image.chatds.network": (
            "none+signed-exact-egress-v3"
        ),
        "org.opencontainers.image.chatds.upstream-unmodified": "true",
        "org.opencontainers.image.chatds.setid-stripped": "true",
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise RuntimeError("deepseek_runner_image_attestation_missing")


def _container_failure_code(container, exit_code: int) -> str:
    """Persist a bounded machine code, never arbitrary container stderr."""

    try:
        payload = container.logs(stdout=False, stderr=True, tail=32)
    except (DockerException, NotFound):
        payload = b""
    for raw in reversed(payload.decode("utf-8", errors="replace").splitlines()):
        try:
            row = json.loads(raw)
        except (TypeError, ValueError):
            continue
        code = str(row.get("code") or "") if isinstance(row, dict) else ""
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", code):
            return code
    return f"runner_process_exited_before_terminal:exit_{exit_code}"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global settings, manager
    settings = load_settings()
    settings.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = docker.from_env()
    client.ping()
    image = client.images.get(settings.runner_image)
    state_host_root = state_volume_host_root(client, settings.state_volume)
    client.volumes.get(settings.egress_proxy_volume)
    client.volumes.get(settings.workspace_lock_volume)
    _verify_runner_image(image)
    manager = Manager(settings, client, state_host_root=state_host_root)
    # This supervisor deliberately does not replay an in-flight model process
    # after its trusted controller has been replaced. Revoke every exact-owned
    # container first; only then may stale rows receive a durable failed
    # terminal. A terminal must never coexist with a worker that can still
    # mutate the Session workspace.
    stale = client.containers.list(
        all=True,
        filters={"label": "chatds.component=deepseek-harness-runner"},
    )
    for container in stale:
        try:
            container.remove(force=True)
        except (DockerException, NotFound) as exc:
            raise RuntimeError(
                "deepseek_stale_runner_revocation_failed"
            ) from exc
    for locator in (settings.state_root / "run-index").glob("*.json"):
        try:
            row = _read_json(locator)
            control = manager._control(row["user_id"], row["conversation_id"], row["run_id"])
            if _terminal(control / "events.jsonl") is None:
                append_terminal(
                    control / "events.jsonl",
                    "failed",
                    "supervisor_restart_before_adoption",
                    error_stage="supervisor_recovery",
                )
        except Exception:
            continue
    yield
    tasks = tuple(manager.tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="ChatDS DeepSeek Harness Runner Supervisor", lifespan=lifespan)


def _auth(x_internal_token: str = Header(default="")) -> None:
    if settings is None or not hmac.compare_digest(
        x_internal_token.encode(), settings.internal_token.encode()
    ):
        raise HTTPException(401, "Unauthorized")


@app.get("/health")
async def health(_=Depends(_auth)):
    if manager is None:
        return JSONResponse(status_code=503, content={"status": "error", "code": "not_ready"})
    try:
        await asyncio.to_thread(manager.client.ping)
        image = await asyncio.to_thread(manager.client.images.get, manager.settings.runner_image)
        _verify_runner_image(image)
    except (DockerException, ImageNotFound):
        return JSONResponse(status_code=503, content={"status": "error", "code": "runtime_unavailable"})
    except RuntimeError:
        return JSONResponse(status_code=503, content={"status": "error", "code": "runtime_attestation_failed"})
    return {
        "status": "ok",
        "deepseek_harness_version": image.labels.get("org.opencontainers.image.version", "unknown"),
        "network_policy": "network-none+signed-exact-egress-v3",
        "workspace_scope": "one-session",
    }


@app.post("/v1/runs")
async def start_run(payload: StartRunRequest, _=Depends(_auth)):
    assert manager is not None
    return await manager.start(payload)


@app.get("/v1/runs/{run_id}/events")
async def run_events(run_id: str, after: int = Query(default=0, ge=0), _=Depends(_auth)):
    assert manager is not None
    return StreamingResponse(
        _stream(manager.event_path(run_id), after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/runs/{run_id}/terminal")
async def run_terminal(
    run_id: str,
    payload: RunIdentity,
    _=Depends(_auth),
):
    assert manager is not None
    return await asyncio.to_thread(manager.terminal, run_id, payload)


@app.post("/v1/runs/{run_id}/approvals/{request_id}")
async def decide_approval(run_id: str, request_id: str, payload: ApprovalDecisionRequest, _=Depends(_auth)):
    assert manager is not None
    return await manager.decide_approval(run_id, request_id, payload)


@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str, payload: RunIdentity, _=Depends(_auth)):
    assert manager is not None
    return {"success": await manager.cancel(run_id, payload)}


@app.post("/v1/runs/{run_id}/controls")
async def control_run(
    run_id: str,
    payload: RunControlRequest,
    _=Depends(_auth),
):
    assert manager is not None
    return await manager.control(run_id, payload)


@app.post("/v1/sessions/{conversation_id}/cleanup")
async def cleanup_session(conversation_id: str, payload: SessionIdentity, _=Depends(_auth)):
    assert manager is not None
    return await manager.cleanup(payload.user_id, conversation_id)
