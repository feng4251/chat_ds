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
    # 10.10.132.2 serves AgentModel (GLM-5.2, 303872 ctx) — 主模型
    # 10.10.132.128 serves qwen3_5 (397B, multimodal) — 多模态识别
    deepseek_pro_base_url: str = "http://10.10.132.2:1025/v1"
    deepseek_pro_api_key: str = "EMPTY"
    qwen3_5_base_url: str = "http://10.10.132.128:1025/v1"
    qwen3_5_api_key: str = "EMPTY"

    # Application
    app_title: str = "Chat ACITS"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174"]

    # Harness agent service
    harness_url: str = "http://harness:8020"
    # HTTPX interprets this as an idle/read boundary, not a whole-workflow
    # duration.  Keep it above the Harness's four-hour absolute provider
    # ceiling so an outer transport cannot sever a healthy silent decoder.
    harness_stream_timeout_seconds: int = 18000
    internal_api_token: str = "chat-ds-internal-token"
    scheduler_poll_seconds: int = 15
    hook_timeout_seconds: int = 8
    allow_private_hook_urls: bool = False
    agent_debug_trace: bool = False
    agent_event_immediate_persist: bool = True

settings = Settings()
