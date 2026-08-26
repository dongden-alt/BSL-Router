"""Compaction skip-regex anchoring.

The skip-regex must be anchored to the start of the model id so that family
tokens only fire when they are the leading token. Models like
`glm-5.3-anthropic` (whose leading token is `glm`, not `anthropic`) must NOT
be skipped — they are the exact models the compaction module targets
(DeepSeek, GLM, MiniMax, Mistral, Kimi, Grok, Qwen).
"""
from __future__ import annotations

from app.middleware.compaction import COMPACTION_SKIP_MODEL_RE


_SKIPS = [
    "claude-opus-5",
    "claude-opus-4.8",
    "claude-sonnet-4-6",
    "gpt-5.6-sol",
    "gpt-5-6-terra",
    "gpt-4o",
    "o3-mini",
    "o1-preview",
    "o4-mini",
    "gemini-3.1-pro",
    "gemini-2.5-flash",
    "vertex-ai",
    "chatgpt-4o",
    "opus-4.8",
    "sonnet-4-6",
    "haiku-5",
    "anthropic-claude-3",
]

_NO_SKIP = [
    "glm-5.3-anthropic",
    "glm-5.2-anthropic",
    "kimi-k3",
    "deepseek-v4-pro",
    "MiniMax-M3",
    "Qwen3.7-Max",
    "kat-coder-pro-v2.5",
    "ox-alpha",
    "mimo-v2.5",
    "mistral-large",
    "grok-3",
]


def test_skip_models_are_skipped():
    for model in _SKIPS:
        assert COMPACTION_SKIP_MODEL_RE.search(model) is not None, (
            f"expected {model!r} to be skipped"
        )


def test_non_skip_models_are_compactable():
    for model in _NO_SKIP:
        assert COMPACTION_SKIP_MODEL_RE.search(model) is None, (
            f"expected {model!r} NOT to be skipped (should be compactable)"
        )
