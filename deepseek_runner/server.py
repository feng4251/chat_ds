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

from .config import ProviderProfile, Settings, load_settings


SAFE_ID = re.compile(r"^[0-9a-f]{32}$")
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_ENTRIES = 200_000


class StartRunRequest(BaseModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    root_run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    conversation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    model_id: str = Field(min_length=1, max_length=128)
    api_model: str = Field(min_length=1, max_length=128)
    provider_profile: str = Field(min_length=1, max_length=64)
    provider_base_url: str = Field(min_length=8, max_length=2048)
    provider_protocol: Literal["openai"]
    messages: list[dict[str, Any]] = Field(max_length=4096)
    tools: list[str] = Field(default_factory=list, max_length=256)
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


def _append_terminal(path: Path, status: str, error: str | None) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line).get("event", {})
            except (ValueError, TypeError):
                continue
            if event.get("type") == "chatds.supervisor.terminal":
                return
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seq = int(json.loads(lines[-1]).get("seq") or 0) + 1 if lines else 1
    envelope = {
        "seq": seq,
        "received_at_unix_ms": int(time.time() * 1000),
        "channel": "supervisor",
        "event": {
            "type": "chatds.supervisor.terminal",
            "status": status,
            "error": error,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        payload = json.dumps(envelope, separators=(",", ":")).encode() + b"\n"
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            offset += os.write(fd, view[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _terminal(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line).get("event", {})
        except (ValueError, TypeError):
            continue
        if event.get("type") == "chatds.supervisor.terminal":
            value = str(event.get("status") or "")
            return value if value in TERMINAL else None
    return None


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


def _prompt(messages: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user").upper()
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        else:
            text = str(content or "")
        rows.append(f"<{role}>\n{text}\n</{role}>")
    return "\n\n".join(rows)


class Manager:
    def __init__(self, settings: Settings, client) -> None:
        self.settings = settings
        self.client = client
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_runs)
        self.guard = asyncio.Lock()
        (settings.state_root / "run-index").mkdir(parents=True, exist_ok=True, mode=0o700)

    def _control(self, user: str, conversation: str, run: str) -> Path:
        return self.settings.state_root / "users" / user / conversation / "runs" / run

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
        try:
            workspace, skill, state = await asyncio.to_thread(self._paths, request)
            receipt = await asyncio.to_thread(verify_skill_view, skill, request.skill_view_sha256)
            web_enabled = "web_search" in request.tools
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
                public_read_enabled=self.settings.public_read_egress_enabled,
            )
            if web_enabled and not any(
                rule.get("url_prefix", "").split("?", 1)[0]
                == self.settings.searxng_search_url
                for rule in policy.get("egress_rules", [])
            ):
                raise RuntimeError("searxng_capability_policy_missing")
            sanitized = {
                "schema": "chatds.deepseek-run.v1",
                **request.model_dump(mode="json"),
                "prompt": _prompt(request.messages),
                "provider_base_url": profile.base_url,
                "searxng_search_url": self.settings.searxng_search_url,
                "web_search_enabled": web_enabled,
                "egress_policy": policy,
            }
            _atomic_json(control / "request.json", sanitized)
            await asyncio.to_thread(_prepare_workspace, workspace, self.settings.worker_gid)
            home = state / "home"
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(home, self.settings.worker_uid, self.settings.worker_gid)
            image = await asyncio.to_thread(self.client.images.get, self.settings.runner_image)
            _verify_runner_image(image)
            volumes = {
                str(workspace): {
                    "bind": "/workspace",
                    "mode": "ro" if request.permission_preset == "read_only" else "rw",
                },
                str(state): {"bind": "/state", "mode": "rw"},
                str(skill): {"bind": "/skill-view", "mode": "ro"},
                str(control): {"bind": "/run/chatds-control", "mode": "rw"},
                str(control / "request.json"): {
                    "bind": "/run/chatds-control/request.json", "mode": "ro",
                },
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
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(container.wait),
                        timeout=self.settings.max_run_seconds,
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
                        _append_terminal(events, "failed", "run_hard_timeout")
                if _terminal(events) is None:
                    _append_terminal(
                        events,
                        "failed",
                        "runner_process_exited_before_terminal" if exit_code else "terminal_missing",
                    )
        except asyncio.CancelledError:
            if container is not None:
                try:
                    await asyncio.to_thread(container.stop, timeout=15)
                except (DockerException, NotFound):
                    pass
            _append_terminal(events, "cancelled", None)
            raise
        except BaseException as exc:
            _append_terminal(events, "failed", (str(exc) or type(exc).__name__)[:128])
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
        _append_terminal(control / "events.jsonl", "cancelled", None)
        return True

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


def _read_event_tail(path: Path, position: int) -> tuple[int, tuple[bytes, ...]]:
    try:
        with path.open("rb") as stream:
            stream.seek(position)
            payload = stream.read()
    except OSError:
        return position, ()
    boundary = payload.rfind(b"\n")
    if boundary < 0:
        return position, ()
    complete = payload[:boundary + 1]
    return position + len(complete), tuple(complete.splitlines())


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
        "org.opencontainers.image.chatds.network": (
            "none+signed-exact-egress-v3"
        ),
        "org.opencontainers.image.chatds.upstream-unmodified": "true",
        "org.opencontainers.image.chatds.setid-stripped": "true",
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise RuntimeError("deepseek_runner_image_attestation_missing")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global settings, manager
    settings = load_settings()
    settings.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = docker.from_env()
    client.ping()
    image = client.images.get(settings.runner_image)
    client.volumes.get(settings.egress_proxy_volume)
    client.volumes.get(settings.workspace_lock_volume)
    _verify_runner_image(image)
    manager = Manager(settings, client)
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
                _append_terminal(control / "events.jsonl", "failed", "supervisor_restart_before_adoption")
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


@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str, payload: RunIdentity, _=Depends(_auth)):
    assert manager is not None
    return {"success": await manager.cancel(run_id, payload)}


@app.post("/v1/sessions/{conversation_id}/cleanup")
async def cleanup_session(conversation_id: str, payload: SessionIdentity, _=Depends(_auth)):
    assert manager is not None
    return await manager.cleanup(payload.user_id, conversation_id)
