"""Fail-closed deployment configuration for the DeepSeek Harness supervisor."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class DeepSeekRunnerConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    id: str
    base_url: str
    api_key: str
    models: frozenset[str]
    context_windows: dict[str, int]


@dataclass(frozen=True, slots=True)
class Settings:
    internal_token: str
    workspace_host_root: Path
    state_root: Path
    runner_image: str
    egress_proxy_volume: str
    workspace_lock_volume: str
    workspace_lock_root: Path
    max_concurrent_runs: int
    max_run_seconds: int
    worker_uid: int
    worker_gid: int
    egress_limits: dict[str, int]
    provider_profiles: dict[str, ProviderProfile]
    private_origin_allowlist: tuple[str, ...]
    public_read_egress_enabled: bool
    searxng_search_url: str


DEFAULT_EGRESS_LIMITS = {
    "max_requests": 8_192,
    "max_outbound_bytes": 64 * 1024 * 1024,
    "max_response_wire_bytes": 2 * 1024 * 1024 * 1024,
}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise DeepSeekRunnerConfigurationError(f"{name} is invalid") from exc
    if value < minimum or value > maximum:
        raise DeepSeekRunnerConfigurationError(f"{name} is outside its bound")
    return value


def _strict_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, str(default).lower()).strip().casefold()
    if raw not in {"true", "false"}:
        raise DeepSeekRunnerConfigurationError(f"{name} must be true or false")
    return raw == "true"


def _absolute_directory(value: str, *, must_exist: bool) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise DeepSeekRunnerConfigurationError("A configured directory is unsafe")
    if must_exist and not path.is_dir():
        raise DeepSeekRunnerConfigurationError("A configured directory is unavailable")
    return path


def _canonical_http_base(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DeepSeekRunnerConfigurationError("Provider base URL is invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _profiles() -> dict[str, ProviderProfile]:
    raw = os.environ.get("DEEPSEEK_HARNESS_PROVIDER_PROFILES_JSON", "").strip()
    if not raw:
        raise DeepSeekRunnerConfigurationError(
            "DEEPSEEK_HARNESS_PROVIDER_PROFILES_JSON is unavailable"
        )
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeepSeekRunnerConfigurationError("Provider profiles are invalid") from exc
    if not isinstance(rows, dict):
        raise DeepSeekRunnerConfigurationError("Provider profiles must be an object")
    result: dict[str, ProviderProfile] = {}
    for profile_id, row in rows.items():
        if not isinstance(profile_id, str) or not profile_id or not isinstance(row, dict):
            raise DeepSeekRunnerConfigurationError("Provider profile identity is invalid")
        if str(row.get("protocol") or "openai") != "openai":
            raise DeepSeekRunnerConfigurationError(
                f"DeepSeek Harness profile {profile_id} is not OpenAI-compatible"
            )
        credential_name = str(row.get("api_key_env") or "")
        credential = os.environ.get(credential_name, "") if credential_name else ""
        models = row.get("models")
        windows = row.get("context_windows")
        if (
            not credential
            or not isinstance(models, list)
            or not models
            or any(not isinstance(model, str) or not model for model in models)
            or not isinstance(windows, dict)
            or set(windows) != set(models)
            or any(
                type(value) is not int or value < 32_000 or value > 4_000_000
                for value in windows.values()
            )
        ):
            raise DeepSeekRunnerConfigurationError(
                f"DeepSeek Harness profile {profile_id} is incomplete"
            )
        result[profile_id] = ProviderProfile(
            id=profile_id,
            base_url=_canonical_http_base(str(row.get("base_url") or "")),
            api_key=credential,
            models=frozenset(models),
            context_windows=dict(windows),
        )
    return result


def load_settings() -> Settings:
    token = os.environ.get("INTERNAL_API_TOKEN", "")
    policy_token = os.environ.get("SKILL_EGRESS_POLICY_TOKEN", "")
    if len(token.encode()) < 16 or len(policy_token.encode()) < 32:
        raise DeepSeekRunnerConfigurationError("Internal authority token is unavailable")
    image = os.environ.get(
        "DEEPSEEK_HARNESS_RUNNER_IMAGE", "chat_ds-deepseek-harness-runner:0.1.0-rc.5"
    ).strip()
    proxy_volume = os.environ.get(
        "DEEPSEEK_HARNESS_EGRESS_PROXY_VOLUME_NAME",
        "chat_ds_skill_egress_proxy_socket",
    ).strip()
    lock_volume = os.environ.get(
        "DEEPSEEK_HARNESS_WORKSPACE_LOCK_VOLUME_NAME",
        "chat_ds_workspace_mutation_locks",
    ).strip()
    if not image or not proxy_volume or not lock_volume:
        raise DeepSeekRunnerConfigurationError("Runtime image or volume is unavailable")
    search_url = _canonical_http_base(
        os.environ.get(
            "DEEPSEEK_HARNESS_SEARXNG_SEARCH_URL", "http://searxng:8080/search"
        )
    )
    if not search_url.endswith("/search"):
        raise DeepSeekRunnerConfigurationError("SearXNG URL must end in /search")
    limit_variables = {
        "max_requests": "DEEPSEEK_HARNESS_EGRESS_MAX_REQUESTS",
        "max_outbound_bytes": "DEEPSEEK_HARNESS_EGRESS_MAX_OUTBOUND_BYTES",
        "max_response_wire_bytes": "DEEPSEEK_HARNESS_EGRESS_MAX_RESPONSE_WIRE_BYTES",
    }
    limits = {
        name: _bounded_int(
            limit_variables[name], default, 1, 16 * 1024 * 1024 * 1024
        )
        for name, default in DEFAULT_EGRESS_LIMITS.items()
    }
    private_origins = tuple(dict.fromkeys(
        value.strip()
        for value in os.environ.get(
            "DEEPSEEK_HARNESS_PRIVATE_ORIGIN_ALLOWLIST", ""
        ).replace(";", ",").split(",")
        if value.strip()
    ))
    return Settings(
        internal_token=token,
        workspace_host_root=_absolute_directory(
            os.environ.get("DEEPSEEK_HARNESS_WORKSPACE_HOST_ROOT", "/nfs/temp/chat_ds"),
            must_exist=True,
        ),
        state_root=_absolute_directory(
            os.environ.get(
                "DEEPSEEK_HARNESS_RUNNER_STATE_ROOT",
                "/var/lib/chatds-deepseek-harness-runner",
            ),
            must_exist=False,
        ),
        runner_image=image,
        egress_proxy_volume=proxy_volume,
        workspace_lock_volume=lock_volume,
        workspace_lock_root=_absolute_directory(
            os.environ.get(
                "WORKSPACE_MUTATION_LOCK_ROOT",
                "/run/chatds-workspace-lock-plane/locks",
            ),
            must_exist=False,
        ),
        max_concurrent_runs=_bounded_int(
            "DEEPSEEK_HARNESS_RUNNER_MAX_CONCURRENT", 4, 1, 64
        ),
        max_run_seconds=_bounded_int(
            "DEEPSEEK_HARNESS_RUNNER_MAX_RUN_SECONDS", 14_400, 60, 86_400
        ),
        worker_uid=_bounded_int(
            "DEEPSEEK_HARNESS_RUNNER_WORKER_UID", 65529, 1, 2**31 - 1
        ),
        worker_gid=_bounded_int(
            "DEEPSEEK_HARNESS_RUNNER_WORKER_GID", 65529, 1, 2**31 - 1
        ),
        egress_limits=limits,
        provider_profiles=_profiles(),
        private_origin_allowlist=private_origins,
        public_read_egress_enabled=_strict_bool(
            "DEEPSEEK_HARNESS_PUBLIC_READ_EGRESS_ENABLED", True
        ),
        searxng_search_url=search_url,
    )
