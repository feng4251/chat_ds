"""Fail-closed deployment configuration for the Claude Runner."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class RunnerConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    id: str
    # These two fields attest the model row received from ChatDS. They may
    # describe the Legacy engine's OpenAI-compatible route; Claude Code itself
    # always uses ``claude_base_url`` through Anthropic Messages.
    backend_base_url: str
    backend_protocol: str
    claude_base_url: str
    api_key: str
    models: frozenset[str]
    # Deployment-owned model capacity.  The Backend repeats this value in a
    # start request so the Supervisor can reject catalog/profile drift before
    # granting a Turn any Docker or Provider authority.
    context_windows: dict[str, int]
    # Anthropic-hosted server tools are not part of the generic Messages
    # compatibility contract. Third-party facades must explicitly attest that
    # they implement Claude Code's native WebSearch/WebFetch semantics.
    native_web_tools: bool = False


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    internal_token: str
    workspace_host_root: Path
    state_root: Path
    runner_image: str
    egress_proxy_volume: str
    workspace_lock_volume: str
    workspace_lock_root: Path
    max_concurrent_runs: int
    preflight_timeout_seconds: float
    max_run_seconds: int
    worker_uid: int
    worker_gid: int
    security_mode: str
    egress_limits: dict[str, int]
    provider_profiles: dict[str, ProviderProfile]
    private_origin_allowlist: tuple[str, ...]


DEFAULT_CLAUDE_EGRESS_LIMITS = {
    "max_requests": 8_192,
    "max_outbound_bytes": 64 * 1024 * 1024,
    "max_response_wire_bytes": 2 * 1024 * 1024 * 1024,
}
_ABSOLUTE_EGRESS_LIMITS = {
    "max_requests": 65_536,
    "max_outbound_bytes": 1024 * 1024 * 1024,
    "max_response_wire_bytes": 16 * 1024 * 1024 * 1024,
}


def load_settings() -> RunnerSettings:
    token = os.environ.get("INTERNAL_API_TOKEN", "")
    if len(token.encode("utf-8")) < 16:
        raise RunnerConfigurationError("INTERNAL_API_TOKEN is unavailable")
    policy_token = os.environ.get("SKILL_EGRESS_POLICY_TOKEN", "")
    if len(policy_token.encode("utf-8")) < 32:
        raise RunnerConfigurationError("SKILL_EGRESS_POLICY_TOKEN is unavailable")
    workspace_root = _absolute_directory(
        os.environ.get("CLAUDE_WORKSPACE_HOST_ROOT", "/nfs/temp/chat_ds"),
        must_exist=True,
    )
    state_root = _absolute_directory(
        os.environ.get("CLAUDE_RUNNER_STATE_ROOT", "/var/lib/chatds-claude-runner"),
        must_exist=False,
    )
    lock_root = _absolute_directory(
        os.environ.get("WORKSPACE_MUTATION_LOCK_ROOT", "/run/chatds-workspace-lock-plane/locks"),
        must_exist=False,
    )
    max_concurrent = _bounded_int("CLAUDE_RUNNER_MAX_CONCURRENT", 4, 1, 64)
    max_run_seconds = _bounded_int("CLAUDE_RUNNER_MAX_RUN_SECONDS", 14400, 60, 86400)
    image = os.environ.get("CLAUDE_RUNNER_IMAGE", "chat_ds-claude-runner:2.1.152").strip()
    volume = os.environ.get(
        "CLAUDE_EGRESS_PROXY_VOLUME_NAME", "chat_ds_skill_egress_proxy_socket"
    ).strip()
    lock_volume = os.environ.get(
        "CLAUDE_WORKSPACE_LOCK_VOLUME_NAME", "chat_ds_workspace_mutation_locks"
    ).strip()
    if (
        not image
        or not volume
        or not lock_volume
        or any(ch.isspace() for ch in image + volume + lock_volume)
    ):
        raise RunnerConfigurationError("Runner image or runtime volume is invalid")
    profiles = _provider_profiles()
    if not profiles:
        raise RunnerConfigurationError("No Claude-compatible provider profile is configured")
    # Browser/user retrieval and deployment-owned private model providers are
    # separate authorities.  The egress compiler still grants only the exact
    # selected Provider's /v1/messages endpoint; listing a Provider origin
    # here does not grant Skills or arbitrary paths access to that host.
    private_values = tuple(dict.fromkeys(
        value.strip()
        for variable in (
            "BROWSER_PRIVATE_ORIGIN_ALLOWLIST",
            "CLAUDE_PROVIDER_PRIVATE_ORIGIN_ALLOWLIST",
        )
        for value in os.environ.get(variable, "").replace(";", ",").split(",")
        if value.strip()
    ))
    security_mode = os.environ.get(
        "CLAUDE_RUNNER_SECURITY_MODE",
        "seccomp_no_new_privileges",
    ).strip()
    if security_mode not in {
        "seccomp_no_new_privileges",
        "seccomp_stripped_setid",
    }:
        raise RunnerConfigurationError("CLAUDE_RUNNER_SECURITY_MODE is invalid")
    egress_limits = _egress_limits(
        "CLAUDE_EGRESS",
        DEFAULT_CLAUDE_EGRESS_LIMITS,
    )
    proxy_ceilings = _egress_limits(
        "CLAUDE_EGRESS_POLICY",
        DEFAULT_CLAUDE_EGRESS_LIMITS,
    )
    if any(
        egress_limits[name] > proxy_ceilings[name]
        for name in DEFAULT_CLAUDE_EGRESS_LIMITS
    ):
        raise RunnerConfigurationError(
            "Claude egress limits exceed proxy policy ceilings"
        )
    return RunnerSettings(
        internal_token=token,
        workspace_host_root=workspace_root,
        state_root=state_root,
        runner_image=image,
        egress_proxy_volume=volume,
        workspace_lock_volume=lock_volume,
        workspace_lock_root=lock_root,
        max_concurrent_runs=max_concurrent,
        preflight_timeout_seconds=float(
            _bounded_int("CLAUDE_RUNNER_PREFLIGHT_TIMEOUT_SECONDS", 1800, 30, 3600)
        ),
        max_run_seconds=max_run_seconds,
        worker_uid=_bounded_int("CLAUDE_RUNNER_WORKER_UID", 65529, 1, 2**31 - 1),
        worker_gid=_bounded_int("CLAUDE_RUNNER_WORKER_GID", 65529, 1, 2**31 - 1),
        security_mode=security_mode,
        egress_limits=egress_limits,
        provider_profiles=profiles,
        private_origin_allowlist=private_values,
    )


def _provider_profiles() -> dict[str, ProviderProfile]:
    raw = os.environ.get("CLAUDE_PROVIDER_PROFILES_JSON", "").strip()
    if raw:
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerConfigurationError("CLAUDE_PROVIDER_PROFILES_JSON is invalid") from exc
        if not isinstance(rows, dict):
            raise RunnerConfigurationError("CLAUDE_PROVIDER_PROFILES_JSON must be an object")
    else:
        rows = {
            "shaiengine": {
                "backend_base_url": os.environ.get(
                    "SHAIENGINE_BASE_URL", "https://api.shaiengine.com/v1"
                ),
                "api_key_env": "SHAIENGINE_API_KEY",
                "backend_protocol": "openai",
                "models": ["glm-5.2", "deepseek-v4-pro"],
                "context_windows": {
                    "glm-5.2": 1_000_000,
                    "deepseek-v4-pro": 200_000,
                },
            }
        }
    profiles: dict[str, ProviderProfile] = {}
    for profile_id, value in rows.items():
        if not isinstance(profile_id, str) or not profile_id or not isinstance(value, dict):
            raise RunnerConfigurationError("Provider profile identity is invalid")
        backend_base = _canonical_http_base(str(value.get("backend_base_url") or ""))
        backend_protocol = str(value.get("backend_protocol") or "openai").strip()
        if backend_protocol not in {"openai", "anthropic"}:
            raise RunnerConfigurationError(
                f"Provider Backend protocol is invalid for {profile_id}"
            )
        claude_base = _canonical_http_base(
            str(value.get("claude_base_url") or _without_v1_suffix(backend_base))
        )
        key_env = str(value.get("api_key_env") or "")
        if not key_env or key_env not in os.environ:
            raise RunnerConfigurationError(f"Provider credential env is unavailable for {profile_id}")
        api_key = os.environ[key_env]
        if not api_key:
            raise RunnerConfigurationError(f"Provider credential is empty for {profile_id}")
        models = value.get("models")
        if not isinstance(models, list) or not models or any(
            not isinstance(item, str) or not item or len(item) > 128 for item in models
        ):
            raise RunnerConfigurationError(f"Provider model allowlist is invalid for {profile_id}")
        context_windows = value.get("context_windows")
        if (
            not isinstance(context_windows, dict)
            or set(context_windows) != set(models)
            or any(
                type(window) is not int
                or window < 200_000
                or window > 4_000_000
                for window in context_windows.values()
            )
        ):
            raise RunnerConfigurationError(
                f"Provider context-window map is invalid for {profile_id}"
            )
        native_web_tools = value.get("native_web_tools", False)
        if type(native_web_tools) is not bool:
            raise RunnerConfigurationError(
                f"Provider native web-tool capability is invalid for {profile_id}"
            )
        profiles[profile_id] = ProviderProfile(
            id=profile_id,
            backend_base_url=backend_base,
            backend_protocol=backend_protocol,
            claude_base_url=claude_base,
            api_key=api_key,
            models=frozenset(models),
            context_windows=dict(context_windows),
            native_web_tools=native_web_tools,
        )
    return profiles


def _without_v1_suffix(value: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, path or "", "", ""))


def _canonical_http_base(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise RunnerConfigurationError("Provider base URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise RunnerConfigurationError("Provider base URL is invalid")
    return value.strip().rstrip("/")


def _absolute_directory(value: str, *, must_exist: bool) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise RunnerConfigurationError("A Runner directory is unsafe")
    if must_exist:
        try:
            if path.resolve(strict=True) != path or not path.is_dir() or path.is_symlink():
                raise RunnerConfigurationError("A Runner directory is unsafe")
        except OSError as exc:
            raise RunnerConfigurationError("A Runner directory is unavailable") from exc
    else:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.resolve(strict=True) != path or path.is_symlink():
            raise RunnerConfigurationError("A Runner directory is unsafe")
    return path


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RunnerConfigurationError(f"{name} is invalid") from exc
    if not minimum <= value <= maximum:
        raise RunnerConfigurationError(f"{name} is out of range")
    return value


def _egress_limits(prefix: str, defaults: dict[str, int]) -> dict[str, int]:
    names = {
        "max_requests": f"{prefix}_MAX_REQUESTS",
        "max_outbound_bytes": f"{prefix}_MAX_OUTBOUND_BYTES",
        "max_response_wire_bytes": f"{prefix}_MAX_RESPONSE_WIRE_BYTES",
    }
    result: dict[str, int] = {}
    for field, environment_name in names.items():
        result[field] = _bounded_int(
            environment_name,
            defaults[field],
            1,
            _ABSOLUTE_EGRESS_LIMITS[field],
        )
    return result
