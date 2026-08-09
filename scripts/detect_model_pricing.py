#!/usr/bin/env python3
"""
Canonical model-pricing detector for BSL Router.

OFFLINE + DETERMINISTIC. Reads `config.yaml`, maps every configured provider
model (and every combo-aliased model) to a single canonical pricing family,
and writes `data/model_pricing_detected.json`.

Variant vs distinct model semantics
-----------------------------------
A VARIANT is the same model at the same price — only a routing alias differs.
Routing modifiers like `-xhigh`, `-high`, `-pro20x`, `-openai-compact`,
`-thinking`, `-antigravity`, `-free`, `-anthropic` are stripped FIRST so all
variants of one model collapse into ONE canonical row with ONE price
(e.g. `gpt-5.5` + its 6 variants → `openai:gpt-5.5` at $5/$30).

A DISTINCT MODEL is a different version/tier with its own price and stays a
separate row (e.g. `kimi-k2.5` ≠ `kimi-k2.6` ≠ `kimi-k2.7-code`;
`gemini-3.1-pro` ≠ `gemini-3.5-flash`). Version number + tier decides identity,
so identical-price siblings that are genuinely different versions can still be
kept apart (GLM-5.1 vs GLM-5.2) while a price-identical version family collapses
to its own canonical family (Opus 4.6 ≠ 4.7 ≠ 4.8 ≠ 5; each is a separate row).

This script NEVER fetches the web. Every canonical family carries either an
embedded first-party price (researched 2026 rates) or `null` prices with a
`source_status` of `manual` / `alias_unverified` — never fabricated as official.

Safe to run repeatedly: output is fully overwritten each run.

Usage:
    python scripts/detect_model_pricing.py
    python scripts/detect_model_pricing.py --config path/to/config.yaml \
        --out path/to/detected.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write(
        "PyYAML is required to run detect_model_pricing.py. "
        "Install it inside the project venv.\n"
    )
    raise

# Default paths (resolved relative to the repo root, not this file's CWD).
_DEFAULT_CONFIG = "config.yaml"
_DEFAULT_OUT = os.path.join("data", "model_pricing_detected.json")


# ─────────────────────────────────────────────────────────────────────────────
# Official / researched price reference ($ per 1M tokens).
# Keyed by canonical family ("provider:canonical_model"). Every family in the
# canonical map lives here — prices are either independently researched or
# operator-approved, while source_status on each rule states confidence.
# Unpriced families carry null prices with a first-party pricing URL only when
# one is available; never fabricate a status of official.
#
# source_status (set per-rule, not here) classifies confidence:
#   official          -> real first-party public price confirmed
#   manual            -> operator-approximated / converted / free tier
#   alias_unverified  -> vendor alias / private / future model, no public price
# ─────────────────────────────────────────────────────────────────────────────
OFFICIAL_PRICES = {
    # ── OpenAI ──────────────────────────────────────────────────────────
    "openai:gpt-5.4": {
        "input_per_1m": 2.50, "output_per_1m": 15.00,
        "cache_hit_per_1m": 0.25, "cache_write_per_1m": 3.125,
        "source_url": "https://platform.openai.com/docs/pricing",
    },
    "openai:gpt-5.4-mini": {
        "input_per_1m": 0.75, "output_per_1m": 4.50,
        "cache_hit_per_1m": 0.075, "cache_write_per_1m": 0.975,
        "source_url": "https://platform.openai.com/docs/pricing",
    },
    "openai:gpt-5.5": {
        "input_per_1m": 5.00, "output_per_1m": 30.00,
        "cache_hit_per_1m": 0.50, "cache_write_per_1m": 6.25,
        "source_url": "https://platform.openai.com/docs/pricing",
    },
    "openai:gpt-5.6-sol": {
        "input_per_1m": 5.00, "output_per_1m": 30.00,
        "cache_hit_per_1m": 0.50, "cache_write_per_1m": 6.25,
        "source_url": "https://platform.openai.com/docs/pricing",
    },
    "openai:gpt-5.6-terra": {
        "input_per_1m": 2.50, "output_per_1m": 15.00,
        "cache_hit_per_1m": 0.25, "cache_write_per_1m": 3.125,
        "source_url": "https://platform.openai.com/docs/pricing",
    },
    "openai:gpt-5.6-luna": {
        "input_per_1m": 1.00, "output_per_1m": 6.00,
        "cache_hit_per_1m": 0.10, "cache_write_per_1m": 1.25,
        "source_url": "https://platform.openai.com/docs/pricing",
    },

    # ── Anthropic ───────────────────────────────────────────────────────
    # Opus 4.6 / 4.7 / 4.8 are identically priced by Anthropic and collapse to 4.8
    # (the latest point release). Only one canonical entry is needed.
    "anthropic:claude-opus-4.8": {
        "input_per_1m": 5.00, "output_per_1m": 25.00,
        "cache_hit_per_1m": 0.50, "cache_write_per_1m": 6.25,
        "source_url": "https://www.anthropic.com/pricing",
    },
    "anthropic:claude-opus-5": {
        "input_per_1m": 5.00, "output_per_1m": 25.00,
        "cache_hit_per_1m": 0.50, "cache_write_per_1m": 6.25,
        "source_url": "https://www.anthropic.com/pricing",
    },
    "anthropic:claude-opus-5-fast": {
        "input_per_1m": 5.00, "output_per_1m": 25.00,
        "cache_hit_per_1m": 0.50, "cache_write_per_1m": 6.25,
        "source_url": "https://www.anthropic.com/pricing",
        "pricing_note": "No separate public pricing for Opus 5 Fast; inherits Opus 5 rates.",
    },
    "anthropic:claude-sonnet-5": {
        "input_per_1m": 2.00, "output_per_1m": 10.00,
        "cache_hit_per_1m": 0.20, "cache_write_per_1m": 2.50,
        "source_url": "https://www.anthropic.com/pricing",
        "pricing_promotion": {
            "active_until": "2026-08-31",
            "post_promotion_rates_per_1m": {
                "input": 3.00,
                "output": 15.00,
                "cache_hit": 0.30,
                "cache_write": 3.75,
            },
        },
        "pricing_note": "User-approved Sonnet 5 promotional rates active through 2026-08-31; post-promotion rates retained for planning.",
    },
    "anthropic:claude-sonnet-4.6": {
        "input_per_1m": 3.00, "output_per_1m": 15.00,
        "cache_hit_per_1m": 0.30, "cache_write_per_1m": 3.75,
        "source_url": "https://www.anthropic.com/pricing",
    },
    "anthropic:claude-fable-5": {
        "input_per_1m": 10.00, "output_per_1m": 50.00,
        "cache_hit_per_1m": 1.00, "cache_write_per_1m": 12.50,
        "source_url": "https://www.anthropic.com/pricing",
    },
    "anthropic:claude-haiku-4.5": {
        "input_per_1m": 1.00, "output_per_1m": 5.00,
        "cache_hit_per_1m": 0.10, "cache_write_per_1m": 1.25,
        "source_url": "https://www.anthropic.com/pricing",
    },

    # ── Google ──────────────────────────────────────────────────────────
    "google:gemini-3.1-pro": {
        "input_per_1m": 2.00, "output_per_1m": 12.00,
        "cache_hit_per_1m": 0.20, "cache_write_per_1m": 2.75,
        "source_url": "https://ai.google.dev/pricing",
    },
    "google:gemini-3.5-flash": {
        "input_per_1m": 1.50, "output_per_1m": 9.00,
        "cache_hit_per_1m": 0.15, "cache_write_per_1m": 1.875,
        "source_url": "https://ai.google.dev/pricing",
    },
    "google:gemini-3-flash": {
        "input_per_1m": 0.50, "output_per_1m": 3.00,
        "cache_hit_per_1m": 0.05, "cache_write_per_1m": 0.625,
        "source_url": "https://ai.google.dev/pricing",
    },
    "google:gemma-4-31b": {
        "input_per_1m": 0.06, "output_per_1m": 0.33,
        "cache_hit_per_1m": 0.006, "cache_write_per_1m": 0.0,
        "source_url": "https://ai.google.dev/pricing",
    },

    # ── DeepSeek ────────────────────────────────────────────────────────
    "deepseek:deepseek-v4-flash": {
        "input_per_1m": 0.14, "output_per_1m": 0.28,
        "cache_hit_per_1m": 0.003, "cache_write_per_1m": 0.0,
        "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
    },
    "deepseek:deepseek-v4-pro": {
        "input_per_1m": 0.435, "output_per_1m": 0.87,
        "cache_hit_per_1m": 0.004, "cache_write_per_1m": 0.0,
        "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
    },

    # ── Zhipu (GLM) ─────────────────────────────────────────────────────
    "zhipu:glm-5.2": {
        "input_per_1m": 1.40, "output_per_1m": 4.40,
        "cache_hit_per_1m": 0.26, "cache_write_per_1m": 0.0,
        "source_url": "https://open.bigmodel.cn/pricing",
    },
    "zhipu:glm-5.1": {
        "input_per_1m": 1.40, "output_per_1m": 4.40,
        "cache_hit_per_1m": 0.18, "cache_write_per_1m": 0.0,
        "source_url": "https://open.bigmodel.cn/pricing",
    },

    # ── Moonshot (Kimi) — THREE distinct models ─────────────────────────
    "moonshot:kimi-k2.5": {
        "input_per_1m": 0.50, "output_per_1m": 2.50,
        "cache_hit_per_1m": 0.12, "cache_write_per_1m": 0.0,
        "source_url": "https://platform.moonshot.ai/pricing",
    },
    "moonshot:kimi-k2.6": {
        "input_per_1m": 0.80, "output_per_1m": 4.00,
        "cache_hit_per_1m": 0.16, "cache_write_per_1m": 0.0,
        "source_url": "https://platform.moonshot.ai/pricing",
    },
    "moonshot:kimi-k2.7-code": {
        "input_per_1m": 0.95, "output_per_1m": 4.00,
        "cache_hit_per_1m": 0.19, "cache_write_per_1m": 0.0,
        "source_url": "https://platform.moonshot.ai/pricing",
    },
    "moonshot:kimi-k3": {
        "input_per_1m": 3.00, "output_per_1m": 15.00,
        "cache_hit_per_1m": 0.30, "cache_write_per_1m": 0.0,
        "source_url": "https://platform.kimi.ai/docs/pricing/chat-k3",
    },

    # ── MiniMax ─────────────────────────────────────────────────────────
    "minimax:minimax-m3": {
        "input_per_1m": 0.30, "output_per_1m": 1.20,
        "cache_hit_per_1m": 0.09, "cache_write_per_1m": 0.0,
        "source_url": "https://platform.minimax.io/pricing",
    },

    # ── Kwaipilot — approved marketplace rate; cache rates unverified ─────
    "kwaipilot:kat-coder-pro-v2": {
        "input_per_1m": 0.30, "output_per_1m": 1.20,
        "cache_hit_per_1m": None, "cache_write_per_1m": None,
        "source_url": "https://openrouter.ai/kwaipilot/kat-coder-pro-v2",
        "pricing_note": "Approved marketplace rate; the OpenRouter listing is a traceable marketplace source, not first-party official pricing. Cache hit/write rates are unverified.",
    },

    # ── Kwaipilot KAT-Coder-Pro V2.5 — official marketplace rate (Jul 10, 2026) ──
    "kwaipilot:kat-coder-pro-v2.5": {
        "input_per_1m": 0.74, "output_per_1m": 2.96,
        "cache_hit_per_1m": None, "cache_write_per_1m": None,
        "source_url": "https://openrouter.ai/kwaipilot/kat-coder-pro-v2.5",
        "pricing_note": "Official OpenRouter marketplace rate. 256K context, 80K max output. Cache rates unverified.",
    },

    # ── Kwaipilot KAT-Coder-Air V2.5 — official marketplace rate (Jul 10, 2026) ──
    "kwaipilot:kat-coder-air-v2.5": {
        "input_per_1m": 0.15, "output_per_1m": 0.60,
        "cache_hit_per_1m": None, "cache_write_per_1m": None,
        "source_url": "https://openrouter.ai/kwaipilot/kat-coder-air-v2.5",
        "pricing_note": "Official OpenRouter marketplace rate. 256K context, 80K max output. Cache rates unverified.",
    },

    # ── xAI ─────────────────────────────────────────────────────────────
    "xai:grok-4.3": {
        "input_per_1m": 1.25, "output_per_1m": 2.50,
        "cache_hit_per_1m": 0.20, "cache_write_per_1m": 0.0,
        "source_url": "https://x.ai/api",
    },

    # ── Xiaomi MiMo ─────────────────────────────────────────────────────
    "xiaomi:mimo-v2.5-pro": {
        "input_per_1m": 0.435, "output_per_1m": 0.87,
        "cache_hit_per_1m": 0.004, "cache_write_per_1m": 0.0,
        "source_url": "https://platform.xiaomimimo.com/pricing",
    },
    "xiaomi:mimo-v2.5-free": {
        "input_per_1m": 0.14, "output_per_1m": 0.28,
        "cache_hit_per_1m": 0.003, "cache_write_per_1m": 0.0,
        "source_url": "https://platform.xiaomimimo.com/pricing",
    },
    "xiaomi:mimo-v2.5-tts-voicedesign": {
        "input_per_1m": 0.0, "output_per_1m": 0.0,
        "cache_hit_per_1m": 0.0, "cache_write_per_1m": 0.0,
        "source_url": "https://platform.xiaomimimo.com/pricing",
    },

    # ── ByteDance ───────────────────────────────────────────────────────
    "bytedance:doubao-seed-2-0-pro": {
        "input_per_1m": 0.67, "output_per_1m": 3.36,
        "cache_hit_per_1m": 0.0, "cache_write_per_1m": 0.0,
        "source_url": "https://www.volcengine.com/product/doubao",
    },

    # ── Alibaba (Qwen) ──────────────────────────────────────────────────
    "alibaba:qwen3.7-max": {
        "input_per_1m": 2.50, "output_per_1m": 3.75,
        "cache_hit_per_1m": 0.25, "cache_write_per_1m": 0.0,
        "source_url": "https://www.alibabacloud.com/help/en/model-studio/getting-started/models",
    },
    "alibaba:qwen3.8-max": {
        "input_per_1m": 2.50, "output_per_1m": 3.75,
        "cache_hit_per_1m": 0.25, "cache_write_per_1m": 0.0,
        "source_url": "https://www.alibabacloud.com/help/en/model-studio/getting-started/models",
        "pricing_note": "Preview model; pricing estimated from Qwen3.7 Max (same tier). No public first-party pricing available yet.",
    },
    "alibaba:qwen3.7-plus": {
        "input_per_1m": 0.40, "output_per_1m": 1.60,
        "cache_hit_per_1m": 0.50, "cache_write_per_1m": 0.0,
        "source_url": "https://www.alibabacloud.com/help/en/model-studio/getting-started/models",
    },
    "alibaba:qwen3.6-plus": {
        "input_per_1m": 0.40, "output_per_1m": 1.20,
        "cache_hit_per_1m": 0.04, "cache_write_per_1m": 0.0,
        "source_url": "https://www.alibabacloud.com/help/en/model-studio/getting-started/models",
    },
    "alibaba:qwen3.5": {
        "input_per_1m": 0.25, "output_per_1m": 0.75,
        "cache_hit_per_1m": 0.025, "cache_write_per_1m": 0.0,
        "source_url": "https://www.alibabacloud.com/help/en/model-studio/getting-started/models",
    },

    # ── NVIDIA ──────────────────────────────────────────────────────────
    "nvidia:nemotron-3-ultra": {
        "input_per_1m": 0.50, "output_per_1m": 2.20,
        "cache_hit_per_1m": 0.10, "cache_write_per_1m": 0.0,
        "source_url": "https://build.nvidia.com",
    },

    # ── Unknown (free tier, no first-party pricing page) ────────────────
    "unknown:north-mini-code-free": {
        "input_per_1m": 0.0, "output_per_1m": 0.0,
        "cache_hit_per_1m": 0.0, "cache_write_per_1m": 0.0,
        "source_url": None,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Routing-suffix modifiers stripped BEFORE family matching.
# Compound suffixes are listed before their substrings; the strip runs in a
# repeat-until-stable loop so chained modifiers (e.g.
# `gpt-5.5-pro20x-openai-compact-antigravity`) all come off.
#
# NOTE: bare `-ultra` is intentionally NOT a standalone suffix — `-ultra` is a
# routing modifier for Claude (`claude-opus-4-6-antigravity-ultra`, handled by
# the `-antigravity-ultra` compound) but is part of the model NAME for Nemotron
# (`nemotron-3-ultra`). Stripping bare `-ultra` would corrupt the Nemotron tier.
#
# NOTE: `-free` is intentionally NOT a universal suffix. It is a routing
# modifier for collapsing families (minimax-m3-free→minimax-m3, glm-5.1-free→
# glm-5.1, deepseek-v4-flash-free→deepseek-v4-flash, qwen3.6-plus-free→
# qwen3.6-plus, nemotron-3-ultra-free→nemotron-3-ultra) but a DISTINCT-MODEL
# marker for `mimo-v2.5-free` and `north-mini-code-free`. Stripping it
# universally would erase those distinct families. Collapsing families instead
# match the `-free` variant inline via a `(-free)?` pattern in their rule.
# ─────────────────────────────────────────────────────────────────────────────
_ROUTING_SUFFIXES = [
    "-antigravity-ultra",
    "-antigravity",
    "-thinking-agentic",
    "-thinking",
    "-agentic",
    "-pro20x-openai-compact",
    "-pro20x",
    "-openai-compact",
    "-xhigh",
    "-high",
    "-anthropic",
]


def _strip_provider_prefix(model_id: str) -> str:
    """Drop a leading `org/` qualifier (e.g. `tanynguyen97/glm-5.2` → `glm-5.2`)."""
    s = str(model_id or "")
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return s


def _clean(model_id: str) -> str:
    """Lowercase, strip provider prefix, then iteratively strip routing suffixes.

    Returns the canonical base id used for family matching.
    """
    s = _strip_provider_prefix(model_id).lower().strip()
    changed = True
    while changed:
        changed = False
        for suf in _ROUTING_SUFFIXES:
            if s.endswith(suf):
                s = s[: -len(suf)]
                changed = True
    return s


def _normalize(model_id: str) -> str:
    """Back-compat alias for `_clean`."""
    return _clean(model_id)


# ─────────────────────────────────────────────────────────────────────────────
# Canonical-family pattern map. Ordered; FIRST match wins against the CLEANED id.
# Each rule: (compiled regex on the cleaned model id, canonical_key,
#             provider_display, canonical_model, display_name, source_status).
# ─────────────────────────────────────────────────────────────────────────────
def _rules():
    R = [
        # ── Anthropic Claude ────────────────────────────────────────────────
        # Fable / fake first (claude-fake-5 is a typo alias for fable-5).
        (r"^claude-fable-5$", "anthropic:claude-fable-5",
         "Anthropic", "claude-fable-5", "Claude Fable 5", "official"),
        (r"^claude-fake-5$", "anthropic:claude-fable-5",
         "Anthropic", "claude-fable-5", "Claude Fable 5", "official"),
        # Sonnet 5 promotional rates are operator-approved; expiry metadata lives
        # in OFFICIAL_PRICES and keeps post-promotion planning rates machine-readable.
        (r"^claude-sonnet-5$", "anthropic:claude-sonnet-5",
         "Anthropic", "claude-sonnet-5", "Claude Sonnet 5", "manual"),
        # Sonnet 4.6.
        (r"^claude-sonnet-4[-.]?6$", "anthropic:claude-sonnet-4.6",
         "Anthropic", "claude-sonnet-4.6", "Claude Sonnet 4.6", "official"),
        # Opus 4.6 / 4.7 / 4.8 are identically-priced variants of the same model
        # family. They collapse to the latest (4.8) as the canonical row.
        # The [-.]? separator handles both dot (opus-4.8) and dash (opus-4-8) forms.
        # The [678] character class matches all three point releases in one rule.
        (r"^(claude-)?opus-4[-.]?[678]$", "anthropic:claude-opus-4.8",
         "Anthropic", "claude-opus-4.8", "Claude Opus 4.8", "official"),
        # Opus 5 Fast — checked before bare Opus 5 so `-fast` isn't lost.
        (r"^(claude-)?opus-5-fast$", "anthropic:claude-opus-5-fast",
         "Anthropic", "claude-opus-5-fast", "Claude Opus 5 Fast", "manual"),
        # Opus 5 — distinct major version, same pricing as 4.8.
        (r"^(claude-)?opus-5$", "anthropic:claude-opus-5",
         "Anthropic", "claude-opus-5", "Claude Opus 5", "official"),
        (r"^claude-haiku-4[-.]?5$", "anthropic:claude-haiku-4.5",
         "Anthropic", "claude-haiku-4.5", "Claude Haiku 4.5", "official"),

        # ── OpenAI GPT ──────────────────────────────────────────────────────
        # Mini before non-mini so gpt-5.4 doesn't swallow gpt-5.4-mini.
        (r"^gpt-5\.4-mini$", "openai:gpt-5.4-mini",
         "OpenAI", "gpt-5.4-mini", "GPT-5.4 mini", "manual"),
        (r"^gpt-5\.4$", "openai:gpt-5.4",
         "OpenAI", "gpt-5.4", "GPT-5.4", "manual"),
        (r"^gpt-5\.5$", "openai:gpt-5.5",
         "OpenAI", "gpt-5.5", "GPT-5.5", "manual"),
        (r"^gpt-5\.6-sol$", "openai:gpt-5.6-sol",
         "OpenAI", "gpt-5.6-sol", "GPT-5.6 Sol", "manual"),
        (r"^gpt-5\.6-terra$", "openai:gpt-5.6-terra",
         "OpenAI", "gpt-5.6-terra", "GPT-5.6 Terra", "manual"),
        (r"^gpt-5\.6-luna$", "openai:gpt-5.6-luna",
         "OpenAI", "gpt-5.6-luna", "GPT-5.6 Luna", "manual"),

        # ── Google Gemini / Gemma ───────────────────────────────────────────
        (r"^gemini-3\.1-pro$", "google:gemini-3.1-pro",
         "Google", "gemini-3.1-pro", "Gemini 3.1 Pro", "manual"),
        (r"^gemini-3\.5-flash$", "google:gemini-3.5-flash",
         "Google", "gemini-3.5-flash", "Gemini 3.5 Flash", "manual"),
        (r"^gemini-3-flash$", "google:gemini-3-flash",
         "Google", "gemini-3-flash", "Gemini 3 Flash", "manual"),
        (r"^gemma-4-31b-it$", "google:gemma-4-31b",
         "Google", "gemma-4-31b", "Gemma 4 31B", "manual"),

        # ── DeepSeek ────────────────────────────────────────────────────────
        # `-free` is a routing modifier here, matched inline (not universal).
        (r"^deepseek-ai-v4-flash(-free)?$", "deepseek:deepseek-v4-flash",
         "DeepSeek", "deepseek-v4-flash", "DeepSeek V4 Flash", "manual"),
        (r"^deepseek-v4-flash(-free)?$", "deepseek:deepseek-v4-flash",
         "DeepSeek", "deepseek-v4-flash", "DeepSeek V4 Flash", "manual"),
        (r"^deepseek-ai-v4-pro$", "deepseek:deepseek-v4-pro",
         "DeepSeek", "deepseek-v4-pro", "DeepSeek V4 Pro", "manual"),
        (r"^deepseek-v4-pro$", "deepseek:deepseek-v4-pro",
         "DeepSeek", "deepseek-v4-pro", "DeepSeek V4 Pro", "manual"),

        # ── Zhipu (GLM) — 5.1 and 5.2 stay distinct ─────────────────────────
        # glm-5.2-anthropic routes here too (-anthropic is a universal suffix).
        (r"^glm-5\.2$", "zhipu:glm-5.2",
         "Zhipu (GLM)", "glm-5.2", "GLM-5.2", "manual"),
        (r"^glm-5\.1(-free)?$", "zhipu:glm-5.1",
         "Zhipu (GLM)", "glm-5.1", "GLM-5.1", "manual"),

        # ── Moonshot (Kimi) — THREE distinct models, ordered most-specific ──
        (r"^kimi-k2\.7-code$", "moonshot:kimi-k2.7-code",
         "Moonshot", "kimi-k2.7-code", "Kimi K2.7 Code", "manual"),
        (r"^kimi-k2\.6$", "moonshot:kimi-k2.6",
         "Moonshot", "kimi-k2.6", "Kimi K2.6", "manual"),
        (r"^kimi-k2\.5$", "moonshot:kimi-k2.5",
         "Moonshot", "kimi-k2.5", "Kimi K2.5", "manual"),
        (r"^kimi-k3$", "moonshot:kimi-k3",
         "Moonshot", "kimi-k3", "Kimi K3", "official"),

        # ── MiniMax ─────────────────────────────────────────────────────────
        # minimax-m3-free routes here (-free is a routing modifier here only).
        (r"^minimax-m3(-free)?$", "minimax:minimax-m3",
         "MiniMax", "minimax-m3", "MiniMax M3", "manual"),
        (r"^minimax-m3$", "minimax:minimax-m3",
         "MiniMax", "minimax-m3", "MiniMax M3", "manual"),

        # ── Kwaipilot ───────────────────────────────────────────────────────
        # Bare and provider-qualified KAT-Coder-Pro-V2 IDs share this approved
        # marketplace-priced family; provider prefixes are removed by `_clean`.
        (r"^kat-coder-pro-v2$", "kwaipilot:kat-coder-pro-v2",
         "Kwaipilot", "kat-coder-pro-v2", "KAT-Coder-Pro-V2", "manual"),

        # KAT-Coder-Pro V2.5 — distinct version, own pricing ($0.74/$2.96).
        # `[-.]?` matches v2.5 (dot), v2-5 (dash), and v25 (bare) forms.
        (r"^kat-coder-pro-v2[-.]?5$", "kwaipilot:kat-coder-pro-v2.5",
         "Kwaipilot", "kat-coder-pro-v2.5", "KAT-Coder-Pro-V2.5", "manual"),

        # KAT-Coder-Air V2.5 — distinct model, own pricing ($0.15/$0.60).
        (r"^kat-coder-air-v2[-.]?5$", "kwaipilot:kat-coder-air-v2.5",
         "Kwaipilot", "kat-coder-air-v2.5", "KAT-Coder-Air-V2.5", "manual"),

        # ── xAI ─────────────────────────────────────────────────────────────
        (r"^grok-4\.3$", "xai:grok-4.3",
         "xAI", "grok-4.3", "Grok-4.3", "manual"),

        # ── Xiaomi MiMo — pro / free / tts all distinct ─────────────────────
        # NOTE: mimo-v2.5-free is its own family; do NOT collapse it onto pro.
        (r"^mimo-v2\.5-tts-voicedesign$", "xiaomi:mimo-v2.5-tts-voicedesign",
         "Xiaomi", "mimo-v2.5-tts-voicedesign", "MiMo v2.5 TTS VoiceDesign", "alias_unverified"),
        (r"^mimo-v2\.5-pro$", "xiaomi:mimo-v2.5-pro",
         "Xiaomi", "mimo-v2.5-pro", "MiMo v2.5 Pro", "manual"),
        (r"^mimo-v2\.5-free$", "xiaomi:mimo-v2.5-free",
         "Xiaomi", "mimo-v2.5-free", "MiMo v2.5 Free", "manual"),

        # ── ByteDance ───────────────────────────────────────────────────────
        (r"^doubao-seed-2-0-pro$", "bytedance:doubao-seed-2-0-pro",
         "ByteDance", "doubao-seed-2-0-pro", "Doubao Seed 2.0 Pro", "manual"),

        # ── Alibaba (Qwen) ──────────────────────────────────────────────────
        # qwen3.6-plus-free collapses onto qwen3.6-plus (-free modifier here).
        (r"^qwen3\.7-max$", "alibaba:qwen3.7-max",
         "Alibaba", "qwen3.7-max", "Qwen3.7 Max", "manual"),
        (r"^qwen3\.8-max(-preview)?$", "alibaba:qwen3.8-max",
         "Alibaba", "qwen3.8-max", "Qwen3.8 Max", "manual"),
        (r"^qwen3\.7-plus$", "alibaba:qwen3.7-plus",
         "Alibaba", "qwen3.7-plus", "Qwen3.7 Plus", "manual"),
        (r"^qwen3\.6-plus(-free)?$", "alibaba:qwen3.6-plus",
         "Alibaba", "qwen3.6-plus", "Qwen3.6 Plus", "manual"),
        (r"^qwen3\.5$", "alibaba:qwen3.5",
         "Alibaba", "qwen3.5", "Qwen3.5", "manual"),

        # ── NVIDIA ──────────────────────────────────────────────────────────
        # nemotron-3-ultra-free collapses here (-free is a routing modifier);
        # `-ultra` is part of the model name, so the universal strip skips it.
        (r"^nemotron-3-ultra(-free)?$", "nvidia:nemotron-3-ultra",
         "NVIDIA", "nemotron-3-ultra", "Nemotron 3 Ultra", "manual"),

        # ── Unknown / other ─────────────────────────────────────────────────
        # north-mini-code-free is its own (unverified) family.
        (r"^north-mini-code-free$", "unknown:north-mini-code-free",
         "Unknown", "north-mini-code-free", "North Mini Code (free)", "alias_unverified"),
    ]
    return [(re.compile(rx, re.IGNORECASE), *rest) for rx, *rest in R]


_RULES = _rules()


def classify(model_id: str):
    """Return (canonical_key, provider_display, canonical_model,
    display_name, source_status) for a model id, or None if no rule matches."""
    cleaned = _clean(model_id)
    if not cleaned:
        return None
    for rx, key, prov, canon, disp, status in _RULES:
        if rx.search(cleaned):
            return key, prov, canon, disp, status
    return None


def _resolve_prices(canonical_key: str, source_status: str):
    """Look up price fields + source_url for a canonical family. Returns a dict
    with the four price fields (None when unknown) and source_url."""
    entry = OFFICIAL_PRICES.get(canonical_key)
    if entry:
        return {
            "input_per_1m": entry.get("input_per_1m"),
            "output_per_1m": entry.get("output_per_1m"),
            "cache_hit_per_1m": entry.get("cache_hit_per_1m"),
            "cache_write_per_1m": entry.get("cache_write_per_1m"),
            "source_url": entry.get("source_url"),
            "pricing_promotion": entry.get("pricing_promotion"),
            "pricing_note": entry.get("pricing_note"),
        }
    # No entry at all — never fabricate.
    return {
        "input_per_1m": None,
        "output_per_1m": None,
        "cache_hit_per_1m": None,
        "cache_write_per_1m": None,
        "source_url": None,
        "pricing_promotion": None,
        "pricing_note": None,
    }


def _load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _iter_config_models(cfg: dict):
    """Yield (provider_id, model_id) for every configured provider model."""
    providers = cfg.get("providers") or {}
    for pid, pdata in providers.items():
        if not isinstance(pdata, dict):
            continue
        for m in pdata.get("models") or []:
            if isinstance(m, dict):
                mid = m.get("id") or m.get("name")
                if mid:
                    yield pid, str(mid)
            elif isinstance(m, str) and m:
                yield pid, m


def _provider_display_name(cfg: dict, provider_id: str) -> str:
    p = (cfg.get("providers") or {}).get(provider_id)
    if isinstance(p, dict) and p.get("name"):
        return str(p["name"])
    return provider_id


def build_detected(cfg: dict) -> dict:
    """Build the canonical_models mapping from a parsed config dict."""
    families = {}  # canonical_key -> accumulator

    def _ensure(key, prov, canon, disp, status):
        fam = families.get(key)
        if fam is None:
            prices = _resolve_prices(key, status)
            fam = {
                "display_name": disp,
                "provider": prov,
                "canonical_model": canon,
                "input_per_1m": prices["input_per_1m"],
                "output_per_1m": prices["output_per_1m"],
                "cache_hit_per_1m": prices["cache_hit_per_1m"],
                "cache_write_per_1m": prices["cache_write_per_1m"],
                "source_status": status,
                "source_url": prices["source_url"],
                **({"pricing_promotion": prices["pricing_promotion"]} if prices["pricing_promotion"] else {}),
                **({"pricing_note": prices["pricing_note"]} if prices["pricing_note"] else {}),
                "patterns": [],
                "providers": [],
                "variants": [],
            }
            families[key] = fam
        return fam

    for pid, mid in _iter_config_models(cfg):
        cls = classify(mid)
        if not cls:
            continue
        key, prov, canon, disp, status = cls
        fam = _ensure(key, prov, canon, disp, status)
        if mid not in fam["patterns"]:
            fam["patterns"].append(mid)
        if pid not in fam["providers"]:
            fam["providers"].append(pid)
        fam["variants"].append({"provider": pid, "provider_name": _provider_display_name(cfg, pid), "model": mid})

    # Finalize: sort patterns/providers/variants, add variant_count.
    out = {}
    for key, fam in families.items():
        fam["patterns"] = sorted(set(fam["patterns"]))
        fam["providers"] = sorted(set(fam["providers"]))
        fam["variants"].sort(key=lambda v: (v["provider"], v["model"]))
        fam["variant_count"] = len(fam["patterns"])
        out[key] = fam
    return {"canonical_models": out}


def run_detection(config_path: str = _DEFAULT_CONFIG, out_path: str = _DEFAULT_OUT) -> dict:
    """Read config, write the detected JSON file, and return the payload."""
    cfg = _load_config(config_path)
    payload = build_detected(cfg)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["config_path"] = config_path
    payload["source"] = "offline-detector"
    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Detect canonical model pricing from config.yaml")
    parser.add_argument("--config", default=_DEFAULT_CONFIG, help="Path to config.yaml")
    parser.add_argument("--out", default=_DEFAULT_OUT, help="Output JSON path")
    args = parser.parse_args(argv)
    payload = run_detection(args.config, args.out)
    n = len(payload.get("canonical_models", {}))
    print(f"Wrote {args.out} ({n} canonical families)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
