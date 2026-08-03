from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    # vLLM provider endpoints (from env)
    deepseek_pro_url: str = "http://10.10.132.2:1025/v1"
    qwen3_5_url: str = "http://10.10.132.128:1025/v1"
    shaiengine_base_url: str = "https://api.shaiengine.com/v1"
    shaiengine_api_key: str = ""

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
    # No-material-progress lease for one child in a delegate_task batch. This is
    # deliberately not an end-to-end batch wall clock: healthy model/tool
    # progress renews it and time spent in the runtime-owned provider-admission
    # queue is exempt. Individual stream/tool limits remain independently
    # bounded.
    delegation_batch_timeout_seconds: float = 3600.0
    # Independent hard deadline for a cooperative async delegate_task batch.
    # Progress and admission waiting never extend it, so a productive complex
    # workflow can outlive the soft lease without becoming unbounded. Arbitrary
    # synchronous code on the event-loop thread is not preemptible; built-in
    # long-running operations must use async I/O or the isolated executors.
    delegation_batch_hard_timeout_seconds: float = 21600.0
    # After revoking a delegated child's execution fence, resource close and
    # task-cancellation acknowledgement each receive this fixed grace. A child
    # which still resists cancellation is isolated under supervision and the
    # parent returns a non-retryable uncertain-state result.
    delegation_cancellation_grace_seconds: float = 5.0
    goal_max_continuations: int = 8
    goal_max_parse_failures: int = 3
    agent_debug_trace: bool = False
    agent_debug_trace_result_chars: int = 4000
    agent_debug_trace_workspace: bool = True
    # One concrete provider stream starts with a bounded no-progress floor.
    # Large requests raise that initial lease to their deterministic
    # input/output budget, and material output may renew it.  The total timeout
    # is the absolute deployment cap; neither planning, progress, nor a caller
    # can extend a request beyond it.
    # This is an absolute ceiling, not the lease granted to every request.
    # Concrete input/output budgets derive a smaller request-specific lease;
    # material progress may renew it only up to this final bound.  Outer
    # transport idle deadlines must remain strictly larger.
    llm_stream_total_timeout_seconds: float = 14400.0
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
    # Keep ordinary production traffic on the SearXNG metasearch boundary.
    # `ddg` is an explicit optional fallback for deployments with direct egress.
    web_search_providers: str = "searxng"
    searxng_base_url: str = "http://searxng:8080"
    searxng_timeout_seconds: float = 10.0
    # Private browser navigation remains disabled unless an origin is present
    # both here and as an explicit URL in the current primary user turn.
    browser_private_origin_allowlist: str = ""
    # Some controlled egress stacks return synthetic addresses for public DNS
    # (for example 198.18.0.0/15). This opt-in is accepted only for DNS names,
    # never literal IP URLs, and only within the fixed synthetic carrier ranges
    # recognized by tools.approval.
    browser_dns_synthetic_public_ranges: str = ""
    # Browser control is fail-closed over a private Unix-domain transport.
    # There is intentionally no local Chromium fallback in tools.browser.
    browser_cdp_socket: str = "/run/chat-ds-browser/cdp.sock"
    browser_cdp_connect_timeout_seconds: float = 10.0

settings = Settings()

# All providers keyed by model_id (the "frontend choice")
PROVIDERS: dict[str, dict] = {
    "shaiengine_glm_5_2": {
        "base_url": settings.shaiengine_base_url,
        "api_model": "glm-5.2",
        "api_key": settings.shaiengine_api_key,
        "provider": "Shaiengine",
        "display_name": "GLM-5.2 (Shaiengine · 默认测试)",
        "is_multimodal": False,
        "is_default": True,
        "agentic_auxiliary_only": False,
        "capabilities": ["text", "tools", "reasoning"],
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
        "thinking_request_format": "thinking_object",
        "thinking_send_enabled_explicitly": True,
        "protocol": "openai",
        # The compatible /models catalog currently omits capacity fields.
        # Keep a conservative static bound and still discover future metadata.
        "context_length": 200000,
        "discover_runtime_metadata": True,
    },
    "shaiengine_deepseek_v4_pro": {
        "base_url": settings.shaiengine_base_url,
        "api_model": "deepseek-v4-pro",
        "api_key": settings.shaiengine_api_key,
        "provider": "Shaiengine",
        "display_name": "DeepSeek V4 Pro (Shaiengine)",
        "is_multimodal": False,
        "is_default": False,
        "agentic_auxiliary_only": False,
        "capabilities": ["text", "tools", "reasoning"],
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
        "thinking_request_format": "thinking_object",
        "thinking_send_enabled_explicitly": True,
        "protocol": "openai",
        "context_length": 200000,
        "discover_runtime_metadata": True,
    },
    "deepseek_v4_pro": {
        "base_url": settings.deepseek_pro_url,
        "api_model": "AgentModel",
        "api_key": "EMPTY",
        "provider": "ZhipuAI",
        "display_name": "GLM-5.2 (本地 AgentModel)",
        "is_multimodal": False,
        "is_default": False,
        "agentic_auxiliary_only": False,
        "capabilities": ["text", "tools", "reasoning"],
        # This vLLM chat template accepts
        # ``chat_template_kwargs.enable_thinking``.  Keep reasoning enabled on
        # ordinary agentic turns; the runtime may disable it only for bounded
        # no-tool recovery/final-synthesis turns that must emit visible text.
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
        "thinking_request_format": "chat_template_kwargs",
        "protocol": "openai",
        "context_length": 303872,
        "discover_runtime_metadata": True,
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
        "thinking_request_format": "chat_template_kwargs",
        "protocol": "openai",
        "context_length": 262144,
        "discover_runtime_metadata": True,
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
    "AgentModel": "deepseek_v4_pro",
}


def canonical_provider_id(model_id: str | None) -> str:
    candidate = str(model_id or DEFAULT_AGENT_MODEL_ID)
    return PROVIDER_ALIASES.get(candidate, candidate)
