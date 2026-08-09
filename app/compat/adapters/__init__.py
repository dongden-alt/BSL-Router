"""
BSL Router — Protocol Adapters

Format adapters that translate between external client dialects and BSL's
internal OpenAI-shaped representation. Each adapter is a set of PURE functions
(no FastAPI / network imports) so it is fully unit-testable in isolation.

Phase 5B-1: `gemini` ports 9Router's Antigravity (Google Cloud Code) MITM
conversion — see `.brain/harvest/antigravity_conversion_spec.md`.
"""
from app.compat.adapters.gemini import (
    unwrap_request,
    is_antigravity,
    normalize_model,
    gemini_request_to_openai,
    openai_chunk_to_gemini,
    openai_response_to_gemini,
    sse_data,
    SSE_DONE,
    build_response_headers,
)

__all__ = [
    "unwrap_request",
    "is_antigravity",
    "normalize_model",
    "gemini_request_to_openai",
    "openai_chunk_to_gemini",
    "openai_response_to_gemini",
    "sse_data",
    "SSE_DONE",
    "build_response_headers",
]
