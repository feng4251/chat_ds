"""Bounded runtime discovery for OpenAI-compatible model capacity metadata.

Static deployment configuration is a fallback, not a durable statement about
the model currently mounted behind an endpoint.  This module reads the
provider's ordinary ``/models`` catalog with a short timeout and a small
singleflight cache.  It never logs or returns endpoint URLs, authorization
headers, or API keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import httpx


_SUCCESS_TTL_SECONDS = 60.0
_FAILURE_TTL_SECONDS = 10.0
_MAX_CACHE_ENTRIES = 64
_REQUEST_TIMEOUT_SECONDS = 3.0
_MAX_CATALOG_BYTES = 1 * 1024 * 1024
_MAX_MODEL_RECORDS = 4_096
_CONTEXT_FIELDS = (
    "max_model_len",
    "context_length",
    "max_context_tokens",
    "context_window",
    "max_seq_len",
)
_OUTPUT_FIELDS = (
    "max_output_tokens",
    "max_completion_tokens",
)


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    metadata: dict[str, int]
    status: str


_CACHE: "OrderedDict[str, _CacheEntry]" = OrderedDict()
_INFLIGHT: dict[str, asyncio.Task[tuple[dict[str, int], str]]] = {}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _provider_key(provider: dict[str, Any]) -> str:
    base_url = str(provider.get("base_url") or "").rstrip("/")
    api_model = str(provider.get("api_model") or provider.get("id") or "")
    api_key = str(provider.get("api_key") or "")
    extra_headers = provider.get("extra_headers")
    if not isinstance(extra_headers, dict):
        extra_headers = {}
    identity = json.dumps(
        {
            "base_url": base_url,
            "api_model": api_model,
            "api_key_sha256": hashlib.sha256(
                api_key.encode("utf-8")
            ).hexdigest(),
            "extra_headers_sha256": hashlib.sha256(
                json.dumps(
                    extra_headers,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _request_headers(provider: dict[str, Any]) -> dict[str, str]:
    raw_extra = provider.get("extra_headers")
    headers = {
        str(key): str(value)
        for key, value in (
            raw_extra.items() if isinstance(raw_extra, dict) else ()
        )
        if isinstance(key, str) and isinstance(value, (str, int, float))
    }
    api_key = str(provider.get("api_key") or "")
    if api_key and api_key != "EMPTY":
        headers.setdefault("Authorization", f"Bearer {api_key}")
    return headers


def _model_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("data")
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _extract_metadata(record: dict[str, Any]) -> dict[str, int]:
    sources = [record]
    for key in ("metadata", "capabilities", "limits"):
        nested = record.get(key)
        if isinstance(nested, dict):
            sources.append(nested)

    context_values = [
        parsed
        for source in sources
        for field in _CONTEXT_FIELDS
        if (parsed := _positive_int(source.get(field))) is not None
    ]
    output_values = [
        parsed
        for source in sources
        for field in _OUTPUT_FIELDS
        if (parsed := _positive_int(source.get(field))) is not None
    ]
    result: dict[str, int] = {}
    if context_values:
        # Conflicting provider fields are resolved conservatively.
        result["context_length"] = min(context_values)
    if output_values:
        result["max_output_tokens"] = min(output_values)
    return result


async def _fetch_provider_metadata(
    provider: dict[str, Any],
) -> tuple[dict[str, int], str]:
    if str(provider.get("protocol") or "openai").lower() != "openai":
        return {}, "unsupported_protocol"
    base_url = str(provider.get("base_url") or "").rstrip("/")
    api_model = str(provider.get("api_model") or provider.get("id") or "")
    if not base_url or not api_model:
        return {}, "invalid_provider_config"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS)
        ) as client:
            async with client.stream(
                "GET",
                f"{base_url}/models",
                headers=_request_headers(provider),
            ) as response:
                response.raise_for_status()
                raw_content_length = response.headers.get("content-length")
                if raw_content_length:
                    try:
                        content_length = int(raw_content_length)
                    except (TypeError, ValueError, OverflowError):
                        content_length = None
                    if (
                        content_length is not None
                        and content_length > _MAX_CATALOG_BYTES
                    ):
                        return {}, "catalog_too_large"

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_CATALOG_BYTES:
                        return {}, "catalog_too_large"
                    body.extend(chunk)
                payload = json.loads(body)
    except Exception:
        # Runtime capacity discovery is advisory.  A provider/client adapter
        # may fail in ways outside httpx's exception hierarchy; none of those
        # failures may prevent the ordinary configured model request.  Do not
        # catch BaseException so task cancellation still propagates normally.
        return {}, "catalog_unavailable"

    raw_records = payload.get("data") if isinstance(payload, dict) else payload
    if (
        isinstance(raw_records, list)
        and len(raw_records) > _MAX_MODEL_RECORDS
    ):
        return {}, "catalog_record_limit_exceeded"
    records = _model_records(payload)
    exact = [
        record for record in records
        if str(record.get("id") or "") == api_model
    ]
    if len(exact) != 1:
        return {}, "model_not_found" if not exact else "model_id_ambiguous"
    metadata = _extract_metadata(exact[0])
    if not metadata:
        return {}, "capacity_fields_missing"
    return metadata, "runtime_catalog"


def _cache_put(key: str, entry: _CacheEntry) -> None:
    _CACHE[key] = entry
    _CACHE.move_to_end(key)
    while len(_CACHE) > _MAX_CACHE_ENTRIES:
        _CACHE.popitem(last=False)


def _discard_inflight_task(
    key: str,
    task: asyncio.Task[tuple[dict[str, int], str]],
) -> None:
    """Drop only the task that still owns this singleflight key."""

    if _INFLIGHT.get(key) is task:
        _INFLIGHT.pop(key, None)


def _merge_provider_error_feedback(
    key: str,
    metadata: dict[str, int],
    status: str,
) -> tuple[dict[str, int], str]:
    """Preserve a stricter limit learned while a catalog fetch was in flight."""

    current = _CACHE.get(key)
    if (
        current is None
        or current.expires_at <= time.monotonic()
        or "provider_error_feedback" not in current.status
    ):
        return dict(metadata), status

    merged = dict(metadata)
    for field in ("context_length", "max_output_tokens"):
        feedback_value = _positive_int(current.metadata.get(field))
        if feedback_value is None:
            continue
        fetched_value = _positive_int(merged.get(field))
        merged[field] = (
            min(fetched_value, feedback_value)
            if fetched_value is not None
            else feedback_value
        )
    merged_status = (
        status
        if "provider_error_feedback" in status
        else f"{status}+provider_error_feedback"
    )
    return merged, merged_status


async def resolve_provider_runtime_metadata(
    provider: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a run-local provider copy enriched by trusted live metadata."""

    resolved = dict(provider)
    key = _provider_key(resolved)
    audit: dict[str, Any] = {
        "provider_key_sha256": key[:20],
        "api_model": str(
            resolved.get("api_model") or resolved.get("id") or ""
        )[:200],
        "enabled": bool(resolved.get("discover_runtime_metadata")),
    }
    if not resolved.get("discover_runtime_metadata"):
        return resolved, {**audit, "status": "disabled"}

    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is not None and cached.expires_at > now:
        _CACHE.move_to_end(key)
        metadata, status = dict(cached.metadata), f"cache:{cached.status}"
    else:
        task = _INFLIGHT.get(key)
        if task is None:
            task = asyncio.create_task(_fetch_provider_metadata(resolved))
            _INFLIGHT[key] = task
            task.add_done_callback(
                lambda completed, cache_key=key: _discard_inflight_task(
                    cache_key,
                    completed,
                )
            )
        try:
            metadata, status = await asyncio.shield(task)
        finally:
            if _INFLIGHT.get(key) is task and task.done():
                _INFLIGHT.pop(key, None)
        metadata, status = _merge_provider_error_feedback(
            key,
            metadata,
            status,
        )
        ttl = _SUCCESS_TTL_SECONDS if metadata else _FAILURE_TTL_SECONDS
        _cache_put(
            key,
            _CacheEntry(
                expires_at=time.monotonic() + ttl,
                metadata=dict(metadata),
                status=status,
            ),
        )

    for field in ("context_length", "max_output_tokens"):
        value = _positive_int(metadata.get(field))
        if value is not None:
            resolved[field] = value
    return resolved, {
        **audit,
        "status": status,
        "context_length": _positive_int(resolved.get("context_length")),
        "max_output_tokens": _positive_int(
            resolved.get("max_output_tokens")
        ),
        "metadata_applied": bool(metadata),
    }


def record_provider_context_limit(
    provider: dict[str, Any],
    context_length: int,
) -> None:
    """Feed an authoritative provider error limit back into the short cache."""

    limit = _positive_int(context_length)
    if limit is None:
        return
    key = _provider_key(provider)
    current = _CACHE.get(key)
    metadata = dict(current.metadata) if current is not None else {}
    existing = _positive_int(metadata.get("context_length"))
    metadata["context_length"] = min(existing, limit) if existing else limit
    _cache_put(
        key,
        _CacheEntry(
            expires_at=time.monotonic() + _SUCCESS_TTL_SECONDS,
            metadata=metadata,
            status="provider_error_feedback",
        ),
    )


def clear_provider_metadata_cache() -> None:
    """Clear bounded process state (used by deterministic tests)."""

    _CACHE.clear()
    for task in tuple(_INFLIGHT.values()):
        task.cancel()
    _INFLIGHT.clear()
