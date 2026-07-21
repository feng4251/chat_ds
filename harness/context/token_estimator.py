"""Shared multimodal-aware prompt token estimation helpers."""

from __future__ import annotations

import json
from typing import Any, Iterable


IMAGE_CONTENT_TYPES = {"image", "image_url", "input_image"}
IMAGE_TOKEN_ESTIMATES = {
    "low": 256,
    "auto": 1024,
    "high": 2048,
}


def estimate_serialized_tokens(value: Any) -> int:
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        serialized = str(value)
    # Four ASCII characters per token is a useful long-text approximation,
    # but UTF-8 bytes/4 undercounts CJK (three-byte characters) and some emoji.
    # Use one token per non-ASCII code point, with an additional byte-based
    # floor for four-byte characters, so compression happens conservatively.
    ascii_chars = sum(1 for char in serialized if ord(char) < 128)
    non_ascii_chars = len(serialized) - ascii_chars
    non_ascii_bytes = sum(
        len(char.encode("utf-8"))
        for char in serialized
        if ord(char) >= 128
    )
    return max(
        1,
        (ascii_chars + 3) // 4
        + max(non_ascii_chars, (non_ascii_bytes + 2) // 3),
    )


def is_image_content_part(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    part_type = str(value.get("type") or "").strip().casefold()
    if part_type in IMAGE_CONTENT_TYPES:
        return True
    image_url = value.get("image_url")
    if isinstance(image_url, (str, dict)):
        return True
    source = value.get("source")
    return bool(
        isinstance(source, dict)
        and str(source.get("media_type") or "").casefold().startswith("image/")
    )


def estimate_image_tokens(value: dict[str, Any]) -> int:
    """Estimate decoded vision cost without counting transport/base64 bytes."""
    detail = str(value.get("detail") or "").strip().casefold()
    image_url = value.get("image_url")
    if isinstance(image_url, dict):
        detail = str(image_url.get("detail") or detail).strip().casefold()
    if detail not in IMAGE_TOKEN_ESTIMATES:
        detail = "auto"

    # Preserve captions/alt/ordinary metadata in the estimate while excluding
    # only fields that carry encoded bytes or a transport URL.
    metadata: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"data", "url"}:
            continue
        if key == "image_url":
            if isinstance(item, dict):
                metadata[key] = {
                    nested_key: nested_value
                    for nested_key, nested_value in item.items()
                    if nested_key not in {"url", "data"}
                }
            continue
        if key == "source" and isinstance(item, dict):
            metadata[key] = {
                nested_key: nested_value
                for nested_key, nested_value in item.items()
                if nested_key not in {"data", "url"}
            }
            continue
        metadata[key] = item
    return IMAGE_TOKEN_ESTIMATES[detail] + estimate_serialized_tokens(metadata)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    content = message.get("content")
    if not isinstance(content, list):
        return estimate_serialized_tokens(message)

    metadata = {key: value for key, value in message.items() if key != "content"}
    estimate = estimate_serialized_tokens(metadata)
    for part in content:
        if is_image_content_part(part):
            estimate += estimate_image_tokens(part)
        else:
            estimate += estimate_serialized_tokens(part)
    return estimate


def estimate_payload_tokens(
    messages: Iterable[dict[str, Any]],
    tool_schemas: Iterable[dict[str, Any]] = (),
) -> int:
    message_list = list(messages)
    schema_list = list(tool_schemas)
    token_estimate = sum(estimate_message_tokens(message) for message in message_list)
    token_estimate += sum(estimate_serialized_tokens(schema) for schema in schema_list)
    return max(
        1,
        token_estimate + len(message_list) * 4 + len(schema_list) * 16,
    )
