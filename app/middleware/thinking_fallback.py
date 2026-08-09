"""
Middleware.thinking_fallback — reseller channel-roulette resilience.

Aggregator/reseller upstreams (e.g. api.iamhc.cn, api.hcnsec.cn) multiplex a
single model id across heterogeneous backend *channels*. One channel may accept
`thinking:{type:"adaptive"}`, another rejects it with a 400
("Unsupported parameter(s): `thinking`"), another only honors `reasoning_effort`.

Because BSL applies a deterministic thinking payload per model-id, a request can
land on an incompatible channel and hard-fail with a terminal 400 that the
5xx/429 combo-fallback never retries.

This module provides PURE helpers to:
  1. Detect a thinking/reasoning parameter rejection from an upstream 400 body.
  2. Detect whether a payload currently carries any thinking/reasoning fields.
  3. Strip all thinking/reasoning fields to produce a widely-accepted payload.

The caller performs ONE degrade-and-retry against the SAME provider: full
payload -> (on rejection) stripped payload. This is stateless, idempotent, and
fail-open: if detection is wrong, the stripped retry simply omits thinking.
"""

from typing import Any, Dict

# Every field BSL may attach across the model families in main.py's thinking map.
# Stripping ALL of them in one pass guarantees a single retry is sufficient
# regardless of which channel/format the reseller routed us onto.
THINKING_PAYLOAD_KEYS = (
    "thinking",          # Anthropic / DeepSeek / Chinese-model style {type: ...}
    "reasoning",         # OpenRouter style {effort, exclude}
    "reasoning_effort",  # OpenAI / DeepSeek style scalar
    "output_config",     # Claude modern / DeepSeek effort container
    "thinking_config",   # legacy Gemini 2.5 (Anthropic-shaped, now stripped)
    "thinkingLevel",     # legacy Gemini 3.x (top-level, now nested)
    "enable_thinking",   # MiMo-style boolean flag
    "generationConfig",  # Gemini native: thinkingConfig container (2.5 + 3.x)
    "includeThoughts",   # Gemini 3.x multi-turn thought-signature flag
)

# Phrases that indicate the upstream rejected a *parameter* it does not accept.
_UNSUPPORTED_MARKERS = (
    "unsupported parameter",
    "unexpected parameter",
    "unknown parameter",
    "unrecognized parameter",
    "unsupported param",
    "invalid parameter",
    "extra fields not permitted",
    "unexpected keyword",
)

# Secondary markers: the rejection specifically names thinking/reasoning.
_THINKING_TERMS = ("thinking", "reasoning")
_REJECTION_VERBS = (
    "not support",
    "unsupported",
    "not allowed",
    "not permitted",
    "invalid",
    "unexpected",
    "unknown",
    "unrecognized",
    "cannot be used",
)


def is_thinking_param_rejection(status_code: int, body_text: str) -> bool:
    """True when a 400 response indicates a thinking/reasoning parameter was rejected.

    Two match paths:
      A. A generic "unsupported parameter" style message (channel rejects extras).
      B. A message that names thinking/reasoning together with a rejection verb.
    """
    if status_code != 400:
        return False
    text = (body_text or "").lower()
    if not text:
        return False

    # Path A: generic unsupported-parameter rejection that also mentions a
    # thinking/reasoning term. Requiring both prevents false-positive retries
    # when a payload happens to carry thinking but the 400 is for a completely
    # different invalid parameter (e.g. bad api_key format).
    if any(marker in text for marker in _UNSUPPORTED_MARKERS) and any(
        term in text for term in _THINKING_TERMS
    ):
        return True

    # Path B: the rejection explicitly names thinking/reasoning.
    if any(term in text for term in _THINKING_TERMS) and any(
        verb in text for verb in _REJECTION_VERBS
    ):
        return True

    return False


def payload_has_thinking(payload: Dict[str, Any]) -> bool:
    """True when the payload carries at least one thinking/reasoning field."""
    if not isinstance(payload, dict):
        return False
    return any(key in payload for key in THINKING_PAYLOAD_KEYS)


def strip_thinking(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of payload with all thinking/reasoning fields removed.

    Does not mutate the input. Safe to call even if no thinking fields exist.
    """
    if not isinstance(payload, dict):
        return payload
    stripped = dict(payload)
    for key in THINKING_PAYLOAD_KEYS:
        stripped.pop(key, None)
    return stripped
