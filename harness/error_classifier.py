"""API error classification for smart recovery.

Provides a structured taxonomy of API errors and a classification pipeline
that determines the correct recovery action (retry, compress context, or abort).

Streamlined from hermes-agent — keeps patterns relevant to vLLM/OAI-compatible
endpoints while dropping provider-specific patterns (Anthropic, OpenRouter, xAI).
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Error taxonomy ──────────────────────────────────────────────────────

class FailoverReason(enum.Enum):
    """Why an API call failed — determines recovery strategy."""

    auth = "auth"
    auth_permanent = "auth_permanent"
    billing = "billing"
    rate_limit = "rate_limit"
    overloaded = "overloaded"
    server_error = "server_error"
    timeout = "timeout"
    context_overflow = "context_overflow"
    payload_too_large = "payload_too_large"
    model_not_found = "model_not_found"
    content_policy_blocked = "content_policy_blocked"
    format_error = "format_error"
    unknown = "unknown"


# ── Classification result ───────────────────────────────────────────────

@dataclass
class ClassifiedError:
    """Structured classification of an API error with recovery hints."""

    reason: FailoverReason
    status_code: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    message: str = ""
    error_context: Dict[str, Any] = field(default_factory=dict)

    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False

    @property
    def is_auth(self) -> bool:
        return self.reason in {FailoverReason.auth, FailoverReason.auth_permanent}


# ── Patterns ────────────────────────────────────────────────────────────

_BILLING_PATTERNS = [
    "insufficient credits",
    "insufficient_quota",
    "insufficient balance",
    "credit balance",
    "credits exhausted",
    "top up your credits",
    "payment required",
    "exceeded your current quota",
    "account is deactivated",
]

_RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttled",
    "requests per minute",
    "tokens per minute",
    "requests per day",
    "try again in",
    "please retry after",
    "resource_exhausted",
    "throttlingexception",
]

_CONTEXT_OVERFLOW_PATTERNS = [
    "context length",
    "context size",
    "maximum context",
    "token limit",
    "too many tokens",
    "reduce the length",
    "exceeds the limit",
    "context window",
    "prompt is too long",
    "max_tokens",
    "maximum number of tokens",
    # vLLM patterns
    "exceeds the max_model_len",
    "max_model_len",
    "prompt length",
    "input is too long",
    "maximum model length",
    # Chinese error messages
    "超过最大长度",
    "上下文长度",
]

_MODEL_NOT_FOUND_PATTERNS = [
    "is not a valid model",
    "invalid model",
    "model not found",
    "model_not_found",
    "does not exist",
    "no such model",
    "unknown model",
    "unsupported model",
]

_CONTENT_POLICY_BLOCKED_PATTERNS = [
    "content_filter",
    "violates our usage policies",
    "your request was flagged by",
    "responsibleaipolicyviolation",
]

_AUTH_PATTERNS = [
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid token",
    "token expired",
]

_TIMEOUT_MESSAGE_PATTERNS = [
    "timed out",
    "request timed out",
    "deadline exceeded",
    "operation timed out",
    "upstream timed out",
]

_TRANSPORT_ERROR_TYPES = frozenset({
    "ReadTimeout", "ConnectTimeout", "PoolTimeout",
    "ConnectError", "RemoteProtocolError",
    "ConnectionError", "ConnectionResetError",
    "ConnectionAbortedError", "BrokenPipeError",
    "TimeoutError", "ReadError",
    "ServerDisconnectedError",
    "SSLError", "SSLZeroReturnError",
    "APIConnectionError", "APITimeoutError",
})

_SERVER_DISCONNECT_PATTERNS = [
    "server disconnected",
    "peer closed connection",
    "connection reset by peer",
    "connection was closed",
    "unexpected eof",
    "incomplete chunked read",
]

_PAYLOAD_TOO_LARGE_PATTERNS = [
    "request entity too large",
    "payload too large",
    "error code: 413",
]

_USAGE_LIMIT_PATTERNS = [
    "usage limit",
    "quota",
    "limit exceeded",
]

_USAGE_LIMIT_TRANSIENT_SIGNALS = [
    "try again",
    "retry",
    "resets at",
    "reset in",
    "wait",
    "requests remaining",
    "periodic",
    "window",
]

_REQUEST_VALIDATION_PATTERNS = [
    "unknown parameter",
    "unsupported parameter",
    "unrecognized request argument",
    "invalid_request_error",
    "unknown_parameter",
    "unsupported_parameter",
]


# ── Classification pipeline ─────────────────────────────────────────────

def classify_api_error(
    error: Exception,
    *,
    provider: str = "",
    model: str = "",
    approx_tokens: int = 0,
    context_length: int = 200000,
    num_messages: int = 0,
) -> ClassifiedError:
    """Classify an API error into a structured recovery recommendation.

    Priority-ordered pipeline:
      1. HTTP status code + message-aware refinement
      2. Error code classification (from body)
      3. Message pattern matching (billing vs rate_limit vs context vs auth)
      4. Server disconnect + large session -> context overflow
      5. Transport error heuristics
      6. Fallback: unknown (retryable with backoff)
    """
    status_code = _extract_status_code(error)
    error_type = type(error).__name__
    if status_code is None and error_type == "RateLimitError":
        status_code = 429
    body = _extract_error_body(error)
    error_code = _extract_error_code(body)

    _raw_msg = str(error).lower()
    _body_msg = ""
    if isinstance(body, dict):
        _err_obj = body.get("error", {})
        if isinstance(_err_obj, dict):
            _body_msg = str(_err_obj.get("message") or "").lower()
        if not _body_msg:
            _body_msg = str(body.get("message") or "").lower()
    parts = [_raw_msg]
    if _body_msg and _body_msg not in _raw_msg:
        parts.append(_body_msg)
    error_msg = " ".join(parts)
    provider_lower = (provider or "").strip().lower()

    def _result(reason: FailoverReason, **overrides) -> ClassifiedError:
        defaults = {
            "reason": reason,
            "status_code": status_code,
            "provider": provider,
            "model": model,
            "message": _extract_message(error, body),
        }
        defaults.update(overrides)
        return ClassifiedError(**defaults)

    # ── 1. Content policy block ────────────────────────────────────
    if any(p in error_msg for p in _CONTENT_POLICY_BLOCKED_PATTERNS):
        return _result(
            FailoverReason.content_policy_blocked,
            retryable=False,
            should_fallback=True,
        )

    # ── 2. HTTP status code classification ─────────────────────────
    if status_code is not None:
        classified = _classify_by_status(
            status_code, error_msg, error_code, body,
            provider=provider_lower,
            approx_tokens=approx_tokens, context_length=context_length,
            num_messages=num_messages,
            result_fn=_result,
        )
        if classified is not None:
            return classified

    # ── 3. Error code classification ───────────────────────────────
    if error_code:
        classified = _classify_by_error_code(error_code, _result)
        if classified is not None:
            return classified

    # ── 4. Message pattern matching (no status code) ───────────────
    classified = _classify_by_message(
        error_msg, error_type,
        approx_tokens=approx_tokens,
        context_length=context_length,
        result_fn=_result,
    )
    if classified is not None:
        return classified

    # ── 5. Server disconnect + large session -> context overflow ───
    is_disconnect = any(p in error_msg for p in _SERVER_DISCONNECT_PATTERNS)
    if is_disconnect and not status_code:
        is_large = approx_tokens > context_length * 0.6 or (
            context_length <= 256000 and (approx_tokens > 120000 or num_messages > 200)
        )
        if is_large:
            return _result(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
            )
        return _result(FailoverReason.timeout, retryable=True)

    # ── 6. Transport / timeout heuristics ──────────────────────────
    if error_type in _TRANSPORT_ERROR_TYPES or isinstance(
        error, (TimeoutError, ConnectionError, OSError)
    ):
        return _result(FailoverReason.timeout, retryable=True)

    # ── 7. Fallback: unknown ───────────────────────────────────────
    return _result(FailoverReason.unknown, retryable=True)


# ── Status code classification ──────────────────────────────────────────

def _classify_by_status(
    status_code: int,
    error_msg: str,
    error_code: str,
    body: dict,
    *,
    provider: str,
    approx_tokens: int,
    context_length: int,
    num_messages: int = 0,
    result_fn,
) -> Optional[ClassifiedError]:
    """Classify based on HTTP status code with message-aware refinement."""

    if status_code == 401:
        return result_fn(
            FailoverReason.auth,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status_code == 403:
        if any(p in error_msg for p in _BILLING_PATTERNS):
            return result_fn(
                FailoverReason.billing,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
            )
        return result_fn(
            FailoverReason.auth,
            retryable=False,
            should_fallback=True,
        )

    if status_code == 402:
        return _classify_402(error_msg, result_fn)

    if status_code == 404:
        if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
            return result_fn(
                FailoverReason.model_not_found,
                retryable=False,
                should_fallback=True,
            )
        return result_fn(FailoverReason.unknown, retryable=True)

    if status_code == 413:
        return result_fn(
            FailoverReason.payload_too_large,
            retryable=True,
            should_compress=True,
        )

    if status_code == 429:
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_fallback=True,
        )

    if status_code == 400:
        return _classify_400(
            error_msg, error_code, body,
            approx_tokens=approx_tokens,
            context_length=context_length,
            num_messages=num_messages,
            result_fn=result_fn,
        )

    if status_code in {500, 502}:
        if (
            any(p in error_msg for p in _REQUEST_VALIDATION_PATTERNS)
            or error_code.lower() in {"invalid_request_error", "unknown_parameter",
                                      "unsupported_parameter"}
        ):
            return result_fn(
                FailoverReason.format_error,
                retryable=False,
                should_fallback=True,
            )
        return result_fn(FailoverReason.server_error, retryable=True)

    if status_code in {503, 529}:
        return result_fn(FailoverReason.overloaded, retryable=True)

    if 400 <= status_code < 500:
        return result_fn(
            FailoverReason.format_error,
            retryable=False,
            should_fallback=True,
        )

    if 500 <= status_code < 600:
        return result_fn(FailoverReason.server_error, retryable=True)

    return None


def _classify_402(error_msg: str, result_fn) -> ClassifiedError:
    """Disambiguate 402: billing exhaustion vs transient usage limit."""
    has_usage_limit = any(p in error_msg for p in _USAGE_LIMIT_PATTERNS)
    has_transient_signal = any(p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS)

    if has_usage_limit and has_transient_signal:
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_fallback=True,
        )

    return result_fn(
        FailoverReason.billing,
        retryable=False,
        should_rotate_credential=True,
        should_fallback=True,
    )


def _classify_400(
    error_msg: str,
    error_code: str,
    body: dict,
    *,
    approx_tokens: int,
    context_length: int,
    num_messages: int = 0,
    result_fn,
) -> ClassifiedError:
    """Classify 400 Bad Request."""

    # Context overflow from 400
    if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    # Model not found as 400
    if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return result_fn(
            FailoverReason.model_not_found,
            retryable=False,
            should_fallback=True,
        )

    # Rate limit / billing as 400
    if any(p in error_msg for p in _RATE_LIMIT_PATTERNS):
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_fallback=True,
        )
    if any(p in error_msg for p in _BILLING_PATTERNS):
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    # Generic 400 + large session -> probable context overflow
    err_body_msg = ""
    if isinstance(body, dict):
        err_obj = body.get("error", {})
        if isinstance(err_obj, dict):
            err_body_msg = str(err_obj.get("message") or "").strip().lower()
        if not err_body_msg:
            err_body_msg = str(body.get("message") or "").strip().lower()
    is_generic = len(err_body_msg) < 30 or err_body_msg in {"error", ""}
    is_large = approx_tokens > context_length * 0.4 or (
        context_length <= 256000 and (approx_tokens > 80000 or num_messages > 80)
    )

    if is_generic and is_large:
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    return result_fn(
        FailoverReason.format_error,
        retryable=False,
        should_fallback=True,
    )


# ── Error code classification ───────────────────────────────────────────

def _classify_by_error_code(
    error_code: str, result_fn,
) -> Optional[ClassifiedError]:
    """Classify by structured error codes from the response body."""
    code_lower = error_code.lower()

    if code_lower in {"resource_exhausted", "throttled", "rate_limit_exceeded"}:
        return result_fn(FailoverReason.rate_limit, retryable=True)

    if code_lower in {
        "insufficient_quota", "billing_not_active", "payment_required",
        "insufficient_credits", "no_usable_credits", "balance_depleted",
    }:
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if code_lower in {"model_not_found", "model_not_available", "invalid_model"}:
        return result_fn(
            FailoverReason.model_not_found,
            retryable=False,
            should_fallback=True,
        )

    if code_lower in {"context_length_exceeded", "max_tokens_exceeded"}:
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    return None


# ── Message pattern classification ──────────────────────────────────────

def _classify_by_message(
    error_msg: str,
    error_type: str,
    *,
    approx_tokens: int,
    context_length: int,
    result_fn,
) -> Optional[ClassifiedError]:
    """Classify based on error message patterns when no status code is available."""

    if any(p in error_msg for p in _PAYLOAD_TOO_LARGE_PATTERNS):
        return result_fn(
            FailoverReason.payload_too_large,
            retryable=True,
            should_compress=True,
        )

    has_usage_limit = any(p in error_msg for p in _USAGE_LIMIT_PATTERNS)
    if has_usage_limit:
        has_transient_signal = any(
            p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS
        )
        if has_transient_signal:
            return result_fn(
                FailoverReason.rate_limit,
                retryable=True,
                should_fallback=True,
            )
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if any(p in error_msg for p in _BILLING_PATTERNS):
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if any(p in error_msg for p in _RATE_LIMIT_PATTERNS):
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_fallback=True,
        )

    if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    if any(p in error_msg for p in _AUTH_PATTERNS):
        return result_fn(
            FailoverReason.auth,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return result_fn(
            FailoverReason.model_not_found,
            retryable=False,
            should_fallback=True,
        )

    if any(p in error_msg for p in _TIMEOUT_MESSAGE_PATTERNS):
        return result_fn(FailoverReason.timeout, retryable=True)

    return None


# ── Helpers ─────────────────────────────────────────────────────────────

def _extract_status_code(error: Exception) -> Optional[int]:
    """Walk the error and its cause chain to find an HTTP status code."""
    current = error
    for _ in range(5):
        code = getattr(current, "status_code", None)
        if isinstance(code, int):
            return code
        code = getattr(current, "status", None)
        if isinstance(code, int) and 100 <= code < 600:
            return code
        cause = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
        if cause is None or cause is current:
            break
        current = cause
    return None


def _extract_error_body(error: Exception) -> dict:
    """Extract the structured error body from an SDK exception."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(error, "response", None)
    if response is not None:
        try:
            json_body = response.json()
            if isinstance(json_body, dict):
                return json_body
        except Exception:
            pass
    return {}


def _extract_error_code(body: dict) -> str:
    """Extract an error code string from the response body."""
    if not body:
        return ""
    error_obj = body.get("error", {})
    if isinstance(error_obj, dict):
        code = error_obj.get("code") or error_obj.get("type") or ""
        if isinstance(code, str) and code.strip() and code.strip() != "400":
            return code.strip()
    code = body.get("code") or body.get("error_code") or ""
    if isinstance(code, (str, int)):
        text = str(code).strip()
        if text and text != "400":
            return text
    return ""


def _extract_message(error: Exception, body: dict) -> str:
    """Extract the most informative error message."""
    if body:
        error_obj = body.get("error", {})
        if isinstance(error_obj, dict):
            msg = error_obj.get("message", "")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:500]
        msg = body.get("message", "")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:500]
    return str(error)[:500]