from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    # vLLM provider endpoints (from env)
    deepseek_pro_url: str = "http://10.10.132.2:1025/v1"
    qwen3_5_url: str = "http://10.10.132.128:1025/v1"

    # Internal model for context compression (auxiliary summarization)
    compressor_model: str = "qwen3_5"
    backend_internal_url: str = "http://backend:8010"
    internal_api_token: str = "chat-ds-internal-token"
    tool_search_mode: str = "auto"
    tool_search_threshold_pct: float = 10.0
    delegation_max_concurrent: int = 3
    delegation_max_iterations: int = 12
    goal_max_continuations: int = 8
    goal_max_parse_failures: int = 3
    agent_debug_trace: bool = False
    agent_debug_trace_result_chars: int = 4000
    agent_debug_trace_workspace: bool = True
    llm_stream_total_timeout_seconds: float = 600.0
    llm_stream_read_timeout_seconds: float = 120.0
    llm_stream_connect_timeout_seconds: float = 30.0
    web_search_providers: str = "searxng,ddg"
    searxng_base_url: str = "http://searxng:8080"
    searxng_timeout_seconds: float = 10.0

settings = Settings()

# All providers keyed by model_id (the "frontend choice")
PROVIDERS: dict[str, dict] = {
    "deepseek_v4_pro": {
        "base_url": settings.deepseek_pro_url,
        "api_model": "AgentModel",
        "api_key": "EMPTY",
        "provider": "ZhipuAI",
        "display_name": "GLM-5.2 (主模型)",
        "is_multimodal": False,
        "is_default": True,
        "capabilities": ["text", "tools", "reasoning"],
        "protocol": "openai",
        "context_length": 303872,
    },
    "qwen3_5": {
        "base_url": settings.qwen3_5_url,
        "api_model": "qwen3_5",
        "api_key": "EMPTY",
        "provider": "Qwen",
        "display_name": "Qwen3-5 (397B 多模态)",
        "is_multimodal": True,
        "is_default": False,
        "capabilities": ["text", "vision", "tools"],
        "protocol": "openai",
        "context_length": 262144,
    },
}
