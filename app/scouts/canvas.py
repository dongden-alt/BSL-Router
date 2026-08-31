"""
Scout.canvas — Canvas/Image Generation Tool Injection

Injects a `generate_image` tool schema into the tools array of an incoming
ChatCompletionRequest. This lets text-only models "draw" by requesting image
generation through the tool interface.

Full execution loop (handling the tool_call response, generating the image,
and feeding it back) will be implemented in Phase 4.
"""

from app.models import ChatCompletionRequest


# The generate_image tool schema injected into requests
GENERATE_IMAGE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Generate an image from a text description. "
            "Use this tool when you need to create, draw, or render any visual content. "
            "Provide a detailed prompt describing the desired image."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "A detailed description of the image to generate.",
                },
                "style": {
                    "type": "string",
                    "description": "Visual style for the image (e.g., realistic, anime, sketch, oil painting).",
                    "enum": ["realistic", "anime", "sketch", "oil_painting", "watercolor", "pixel_art", "3d_render"],
                },
                "size": {
                    "type": "string",
                    "description": "Image dimensions.",
                    "enum": ["256x256", "512x512", "1024x1024"],
                },
            },
            "required": ["prompt"],
        },
    },
}


