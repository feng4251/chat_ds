"""Image generation tool using fal_client API.

Provides image_generate for text-to-image generation. Uses fal_client
with FAL_KEY from environment for authentication.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

IMAGE_GENERATE_SCHEMA = {
    "name": "image_generate",
    "description": (
        "Generate an image from a text prompt. Returns a URL to the generated image. "
        "You can optionally specify an aspect ratio or image size. "
        "Valid aspect ratios: square (1:1), landscape (16:9), portrait (9:16)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text prompt describing the image to generate.",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["1:1", "16:9", "9:16"],
                "description": "Aspect ratio for the generated image (default '1:1').",
            },
        },
        "required": ["prompt"],
    },
}


async def image_generate(
    prompt: str,
    aspect_ratio: str = "1:1",
    session_id: str = "default",
) -> str:
    """Generate an image using fal_client with flux/schnell model.

    Args:
        prompt: Text prompt describing the image.
        aspect_ratio: Aspect ratio (1:1, 16:9, 9:16).
        session_id: Session identifier (unused, for signature compatibility).

    Returns:
        Image URL or error message.
    """
    try:
        import fal_client
    except ImportError:
        return (
            "Image generation is not available: fal_client package is not installed. "
            "Please install it with: pip install fal-client"
        )

    # Map aspect ratio to fal image_size
    size_map = {
        "1:1": "square_hd",
        "16:9": "landscape_16_9",
        "9:16": "portrait_16_9",
    }
    image_size = size_map.get(aspect_ratio, "square_hd")

    try:
        result = await fal_client.run_async(
            "fal-ai/flux/schnell",
            arguments={
                "prompt": prompt,
                "image_size": image_size,
                "num_inference_steps": 4,
            },
        )

        image_url = None
        if isinstance(result, dict):
            images = result.get("images", [])
            if images and isinstance(images[0], dict):
                image_url = images[0].get("url")
            elif images:
                image_url = str(images[0])
        elif isinstance(result, list) and result:
            image_url = str(result[0])

        if image_url:
            return (
                f"Image generated successfully.\n"
                f"Prompt: {prompt}\n"
                f"Aspect ratio: {aspect_ratio}\n"
                f"Image URL: {image_url}\n\n"
                f"You can display this image to the user with markdown: ![generated image]({image_url})"
            )

        return f"Image generation completed but no image URL was returned. Raw result: {result}"

    except Exception as e:
        logger.error("Image generation failed: %s", e)
        return f"Image generation failed: {e}"