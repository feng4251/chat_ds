"""Image routing for chat_ds harness — ported from hermes-agent.

Since deepseek_v4_pro (primary) is a text-only model, all
user-attached images are routed through the **text** pipeline:

  1. Pre-analyze each image with qwen3_5 (vision model)
  2. Prepend text descriptions to the user message
  3. The agent still has ``vision_analyze`` tool for deeper inspection
  4. Session skills and MCP tools are discovered by the agent via
     ``skills_list`` / ``skill_view`` — the agent decides whether to
     call MCP tools or vision_analyze based on skill instructions

This is the same pattern as hermes-agent's ``_enrich_message_with_vision``
in ``gateway/run.py``.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

import httpx

from config import PROVIDERS

logger = logging.getLogger(__name__)

# Maximum characters for a single image description in the enriched message.
MAX_DESCRIPTION_CHARS = 1500

# Maximum base64 payload size for vision API calls (20 MB).
# Ported from hermes-agent/tools/vision_tools.py.
_MAX_BASE64_BYTES = 20 * 1024 * 1024


async def _analyze_image_with_vision(
    image_url: str,
    question: str = "Describe everything visible in this image in thorough detail.",
) -> str:
    """Call qwen3_5 to analyze a single image. Returns text description."""
    provider = PROVIDERS.get("qwen3_5")
    if not provider:
        return "[Vision model not available — cannot analyze image.]"

    # Quick size check for data URLs to avoid sending huge payloads.
    if isinstance(image_url, str) and image_url.startswith("data:"):
        if len(image_url) > _MAX_BASE64_BYTES:
            logger.warning(
                "Image data URL too large for vision: %.1f MB (limit %.0f MB)",
                len(image_url) / (1024 * 1024), _MAX_BASE64_BYTES / (1024 * 1024),
            )
            return (
                f"[图片过大无法自动分析 (base64: {len(image_url) / (1024 * 1024):.1f} MB, "
                f"上限 {_MAX_BASE64_BYTES / (1024 * 1024):.0f} MB)。"
                f"请压缩图片后重试，或使用 vision_analyze 工具手动分析。]"
            )

    base_url = provider["base_url"]
    api_model = provider["api_model"]
    api_key = provider.get("api_key", "EMPTY")

    headers = {"Content-Type": "application/json"}
    if api_key and api_key != "EMPTY":
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": api_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": question},
                ],
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            if resp.status_code != 200:
                err_text = resp.text[:300]
                logger.warning("Vision pre-analysis failed: HTTP %s: %s", resp.status_code, err_text)
                return f"[Image analysis failed: HTTP {resp.status_code}]"

            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if content:
                    return content[:MAX_DESCRIPTION_CHARS]
            return "[Image analysis returned no content.]"
    except Exception as e:
        logger.warning("Vision pre-analysis error: %s", e)
        return f"[Image analysis error: {e}]"


async def enrich_message_with_vision(
    user_text: str,
    image_urls: List[str],
    session_id: str = "default",
    user_id: str = "default",
) -> str:
    """Pre-analyze user-attached images and prepend descriptions to the message.

    This is the text-mode pipeline: each image is analyzed with qwen3_5,
    and the resulting description is prepended to the user's text. The
    text-only main model (deepseek_v4_pro) never sees pixels — only the lossy
    text summary.

    CRITICAL: The full image data URL is saved to a session file so the
    agent can pass it to vision_analyze or MCP tools on subsequent turns.
    The pre-analysis text is a HINT, not a substitute for domain-specific
    tools (pathology MCP, OCR, etc.).

    Args:
        user_text: The user's original message text.
        image_urls: List of image URLs (data: URLs from frontend).
        session_id: Session identifier for file storage.
        user_id: User identifier for file storage.

    Returns:
        Enriched message string with vision descriptions prepended.
    """
    if not image_urls:
        return user_text

    enriched_parts: list[str] = []
    saved_image_paths: list[str] = []

    # Save images to session workspace so they persist across turns
    for i, url in enumerate(image_urls):
        try:
            # Save the data URL to a session file for persistence
            img_path = _save_image_for_session(url, i, user_id, session_id)
            if img_path:
                saved_image_paths.append(img_path)
        except Exception as e:
            logger.warning("Failed to save image %d for session: %s", i + 1, e)

    for i, url in enumerate(image_urls):
        try:
            logger.debug("Pre-analyzing user image %d: %s...", i + 1, url[:60])
            description = await _analyze_image_with_vision(url)

            img_ref = ""
            if i < len(saved_image_paths) and saved_image_paths[i]:
                img_ref = (
                    f"\n[图片已保存至: {saved_image_paths[i]}]\n"
                    f"[如需调用 pathology 等 MCP skill 处理此图片，"
                    f"使用 skill_view 查看 skill 指令后，"
                    f"将图片路径传给对应的 MCP 工具]"
                )
            else:
                # Fallback: include truncated URL reference
                url_preview = url[:100] if len(url) > 100 else url
                img_ref = (
                    f"\n[如需更详细分析，可使用 vision_analyze 工具，"
                    f"image_url 前缀: {url_preview}...]"
                )

            enriched_parts.append(
                f"[用户上传了图片 {i + 1}，通用视觉预分析如下"
                f"（注意：此为通用描述，非领域专用分析）：\n{description}]"
                f"{img_ref}"
            )
        except Exception as e:
            logger.error("Vision pre-analysis error for image %d: %s", i + 1, e)
            enriched_parts.append(
                f"[用户上传了图片 {i + 1}，但自动分析失败。"
                f"可使用 vision_analyze 工具手动分析。]"
            )

    # Combine: vision descriptions first, then the user's original text
    if enriched_parts:
        prefix = "\n\n".join(enriched_parts)
        if user_text:
            return f"{prefix}\n\n用户消息: {user_text}"
        return prefix
    return user_text


def _save_image_for_session(
    image_url: str,
    index: int,
    user_id: str,
    session_id: str,
) -> str:
    """Save a data URL image to the session workspace for persistence.

    Returns the file path relative to the sandbox, or empty string on failure.
    """
    import base64
    import os
    from pathlib import Path

    try:
        if not image_url.startswith("data:"):
            return image_url  # Already a path/URL, return as-is

        # Parse data URL: data:image/png;base64,<data>
        header, b64_data = image_url.split(",", 1)
        mime_type = "image/png"
        if "image/" in header:
            mime_type = header.split("image/")[1].split(";")[0]

        ext = mime_type.split("+")[0]  # handle svg+xml
        if ext == "jpeg":
            ext = "jpg"

        raw = base64.b64decode(b64_data)

        # Save to session sandbox
        sandbox_base = Path("/nfs/temp/chat_ds")
        session_dir = sandbox_base / user_id / session_id / "workspace"
        session_dir.mkdir(parents=True, exist_ok=True)

        filename = f"uploaded_image_{index}.{ext}"
        filepath = session_dir / filename
        filepath.write_bytes(raw)

        logger.info("Saved image %d to %s (%d bytes)", index, filepath, len(raw))
        return str(filepath)
    except Exception as e:
        logger.warning("Failed to save image %d: %s", index, e)
        return ""


def strip_images_from_messages(messages: list[dict]) -> bool:
    """Remove image_url content parts from all messages in-place.

    Used as a safety net when a text-only model rejects messages containing
    image content. Returns True if any images were stripped.
    """
    stripped = False
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_content = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                stripped = True
                continue
            new_content.append(part)
        if len(new_content) != len(content):
            msg["content"] = new_content
    return stripped


# Image-rejection phrases for detection in error responses.
# Ported from hermes-agent/agent/conversation_loop.py.
_IMAGE_REJECTION_PHRASES = (
    "only 'text' content type is supported",
    "only text content type is supported",
    "image_url is not supported",
    "image content is not supported",
    "multimodal is not supported",
    "multimodal content is not supported",
    "multimodal input is not supported",
    "vision is not supported",
    "vision input is not supported",
    "does not support images",
    "does not support image input",
    "does not support multimodal",
    "does not support vision",
    "model does not support image",
    "unknown variant `image_url`, expected `text`",
    "unknown variant image_url, expected text",
)


def is_image_rejection_error(error_body: str, status_code: int | None = None) -> bool:
    """Detect if an API error is due to the model rejecting image content.

    Only matches 4xx errors (client errors), not 5xx (server errors).
    """
    if status_code is not None and (status_code < 400 or status_code >= 500):
        return False
    err_lower = error_body.lower()
    return any(phrase in err_lower for phrase in _IMAGE_REJECTION_PHRASES)


__all__ = [
    "enrich_message_with_vision",
    "strip_images_from_messages",
    "is_image_rejection_error",
]
