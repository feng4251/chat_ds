from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    # JWT
    secret_key: str = "chat-ds-secret-key-change-in-production-2026"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24h

    # Database
    database_url: str = "sqlite+aiosqlite:///./chat_ds.db"

    # Default model endpoints (intranet)
    # These independent deployments may expose the same wire model id.  Their
    # endpoint and capacity identities must remain separate in the catalog.
    # 10.10.132.2 serves AgentModel (GLM-5.2, 918528 ctx) — 主模型
    # 10.10.132.126 serves AgentModel (DeepSeek-V4-Flash, 1048576 ctx)
    # 10.10.132.128 serves qwen3_5 (397B, multimodal) — 多模态识别
    deepseek_pro_base_url: str = "http://10.10.132.2:1025/v1"
    deepseek_pro_api_key: str = "EMPTY"
    local_deepseek_v4_flash_base_url: str = "http://10.10.132.126:1025/v1"
    local_deepseek_v4_flash_api_key: str = "EMPTY"
    qwen3_5_base_url: str = "http://10.10.132.128:1025/v1"
    qwen3_5_api_key: str = "EMPTY"
    shaiengine_base_url: str = "https://api.shaiengine.com/v1"
    shaiengine_api_key: str = ""

    # Application
    app_title: str = "Chat ACITS"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174"]

    # Harness agent service
    harness_url: str = "http://harness:8020"
    # HTTPX interprets this as an idle/read boundary, not a whole-workflow
    # duration.  Keep it above the Harness's four-hour absolute provider
    # ceiling so an outer transport cannot sever a healthy silent decoder.
    harness_stream_timeout_seconds: int = 18000
    # Optional second execution engine. It is fail-closed unless the trusted
    # Runner Supervisor and its networkless per-Turn container boundary are
    # explicitly enabled by deployment configuration.
    claude_code_engine_enabled: bool = False
    # Keep the adapter and historical Sessions readable while allowing a
    # deployment to route every newly executed Turn through Claude Code.
    legacy_engine_new_runs_enabled: bool = True
    deepseek_harness_engine_enabled: bool = False
    default_agent_engine_id: Literal[
        "legacy", "claude_code", "deepseek_harness"
    ] = "legacy"
    claude_runner_url: str = "http://claude-runner-supervisor:8030"
    claude_runner_stream_timeout_seconds: int = 18000
    claude_code_provider_profiles: list[str] = ["shaiengine"]
    deepseek_harness_provider_profiles: list[str] = ["shaiengine"]
    deepseek_runner_url: str = "http://deepseek-runner-supervisor:8040"
    deepseek_runner_stream_timeout_seconds: int = 18000
    claude_web_search_url: str = "http://searxng:8080/search"
    claude_market_data_url: str = (
        "http://market-data-gateway:8090/v1/quote"
    )
    internal_api_token: str = "chat-ds-internal-token"
    scheduler_poll_seconds: int = 15
    hook_timeout_seconds: int = 8
    allow_private_hook_urls: bool = False
    agent_debug_trace: bool = False
    agent_event_immediate_persist: bool = True

    @model_validator(mode="after")
    def validate_default_agent_engine(self):
        if (
            self.default_agent_engine_id == "claude_code"
            and not self.claude_code_engine_enabled
        ):
            raise ValueError(
                "DEFAULT_AGENT_ENGINE_ID=claude_code requires "
                "CLAUDE_CODE_ENGINE_ENABLED=true"
            )
        if (
            self.default_agent_engine_id == "legacy"
            and not self.legacy_engine_new_runs_enabled
        ):
            raise ValueError(
                "DEFAULT_AGENT_ENGINE_ID=legacy requires "
                "LEGACY_ENGINE_NEW_RUNS_ENABLED=true"
            )
        if (
            self.default_agent_engine_id == "deepseek_harness"
            and not self.deepseek_harness_engine_enabled
        ):
            raise ValueError(
                "DEFAULT_AGENT_ENGINE_ID=deepseek_harness requires "
                "DEEPSEEK_HARNESS_ENGINE_ENABLED=true"
            )
        return self

settings = Settings()
