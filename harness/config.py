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
    # Optional process-local admission for agent-loop model HTTP requests
    # (primary, delegate, fan-in reducer, and bounded control-plane calls).
    # Auxiliary qwen compression/vision paths keep their own bounded clients
    # and are intentionally outside the AgentModel capacity budget. Zero
    # disables the corresponding limit so non-compose deployments retain
    # historical behavior. Deployment capacity belongs in environment
    # configuration, not in Skill/workflow policy.
    provider_admission_max_inflight_requests: int = 0
    provider_admission_max_inflight_estimated_tokens: int = 0
    provider_admission_estimate_safety_factor: float = 1.0
    provider_admission_wait_timeout_seconds: float = 0.0
    # Skill HTTP remains bounded, but evidence workflows commonly need more
    # than eight serial pages. Runtime consumers clamp these settings to
    # conservative hard maxima even if an environment value is malformed or
    # excessive.
    skill_http_max_requests_per_run: int = 16
    delegated_retrieval_max_pages_per_chain: int = 12
    delegated_retrieval_max_total_response_bytes: int = 2_400_000
    delegated_retrieval_max_total_request_seconds: float = 360.0
    # End-to-end wall clock bound for one delegate_task batch. Individual
    # model/tool calls retain their own shorter timeouts; this outer deadline
    # prevents one wedged child from holding every completed sibling behind an
    # unbounded gather barrier.
    # A batch may contain several long-context children while provider token
    # admission intentionally runs only a subset concurrently.  Keep the
    # outer envelope longer than one stream deadline/cohort; child iteration,
    # stream, retrieval, and convergence bounds remain independently strict.
    delegation_batch_timeout_seconds: float = 3600.0
    goal_max_continuations: int = 8
    goal_max_parse_failures: int = 3
    agent_debug_trace: bool = False
    agent_debug_trace_result_chars: int = 4000
    agent_debug_trace_workspace: bool = True
    # One concrete provider stream starts with a bounded no-progress lease, but
    # may use a larger deterministic allowance when its real input/output token
    # budget warrants it and material model output keeps arriving.  The total
    # timeout is the absolute deployment cap; neither progress nor a caller can
    # extend a request beyond it.
    llm_stream_total_timeout_seconds: float = 1500.0
    llm_stream_initial_timeout_seconds: float = 600.0
    llm_stream_progress_grace_seconds: float = 180.0
    llm_stream_input_planning_tokens_per_second: float = 256.0
    llm_stream_output_planning_tokens_per_second: float = 8.0
    llm_stream_planning_safety_factor: float = 1.10
    llm_stream_fixed_overhead_seconds: float = 30.0
    llm_stream_read_timeout_seconds: float = 120.0
    llm_stream_connect_timeout_seconds: float = 30.0
    # A corrupt streamed tool-call batch gets at most one non-stream repair.
    # Keep that bounded recovery independent from the adaptive stream budget:
    # increasing the latter for long, productive generations must not turn a
    # control-plane repair into a 25-minute blocking request.
    llm_nonstream_repair_timeout_seconds: float = 600.0
    complex_report_max_iterations: int = 160
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
        "agentic_auxiliary_only": False,
        "capabilities": ["text", "tools", "reasoning"],
        # This vLLM chat template accepts
        # ``chat_template_kwargs.enable_thinking``.  Keep reasoning enabled on
        # ordinary agentic turns; the runtime may disable it only for bounded
        # no-tool recovery/final-synthesis turns that must emit visible text.
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
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
        # Qwen is retained for explicit direct multimodal turns and bounded
        # auxiliary work (vision enrichment and context compression).  It is
        # not an implicit fallback for Skill workflows or delegated agents.
        "agentic_auxiliary_only": True,
        "capabilities": ["text", "vision", "tools"],
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": False,
        "protocol": "openai",
        "context_length": 262144,
    },
}

_default_provider_ids = [
    model_id for model_id, config in PROVIDERS.items()
    if config.get("is_default") is True
]
if len(_default_provider_ids) != 1:
    raise RuntimeError(
        "Harness model catalog must declare exactly one default provider; got "
        + repr(_default_provider_ids)
    )

DEFAULT_AGENT_MODEL_ID = _default_provider_ids[0]
PROVIDER_ALIASES = {
    # Historic API/persisted identifier. Aliases are normalized at ingress and
    # never appear as duplicate entries in the model catalog.
    "AgentModel": DEFAULT_AGENT_MODEL_ID,
}


def canonical_provider_id(model_id: str | None) -> str:
    candidate = str(model_id or DEFAULT_AGENT_MODEL_ID)
    return PROVIDER_ALIASES.get(candidate, candidate)
