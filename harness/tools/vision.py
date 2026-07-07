"""Vision analysis tool using qwen3_5 multimodal model.

Provides vision_analyze for image understanding. Sends an image URL to the
qwen3_5 vLLM endpoint and returns text analysis.
"""

from __future__ import annotations

import logging

import httpx

from config import PROVIDERS

logger = logging.getLogger(__name__)

VISION_ANALYZE_SCHEMA = {
    "name": "vision_analyze",
    "description": (
        "Analyze an image using a vision-capable model. Provide an image URL "
        "and optionally a question about the image. Returns a text description "
        "or answer about the image content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "image_url": {
                "type": "string",
                "description": "URL of the image to analyze (data: URLs are also supported).",
            },
            "question": {
                "type": "string",
                "description": "Optional question about the image. If not provided, the model will describe the image.",
            },
        },
        "required": ["image_url"],
    },
}


async def vision_analyze(
    image_url: str,
    question: str = "Describe this image in detail.",
    session_id: str = "default",
) -> str:
    """Analyze an image using qwen3_5 multimodal model.

    Args:
        image_url: URL of the image (data: URLs supported).
        question: What to ask about the image.
        session_id: Session identifier (unused, for signature compatibility).

    Returns:
        Text analysis of the image.
    """
    provider = PROVIDERS.get("qwen3_5")
    if not provider:
        return "Vision analysis is not available: qwen3_5 provider not configured."

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
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                    {
                        "type": "text",
                        "text": question,
                    },
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
                err_text = resp.text[:500]
                logger.error("Vision analysis failed: HTTP %s: %s", resp.status_code, err_text)
                return f"Vision analysis failed: HTTP {resp.status_code}: {err_text}"

            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if content:
                    return content
            return f"Vision analysis returned no content. Raw: {data}"
    except httpx.TimeoutException:
        return "Vision analysis timed out."
    except Exception as e:
        logger.error("Vision analysis failed: %s", e)
        return f"Vision analysis failed: {e}"