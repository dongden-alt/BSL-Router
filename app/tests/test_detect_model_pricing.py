"""Tests for the offline canonical model-pricing detector.

These verify the CORRECTED variant-vs-distinct-model semantics:

* VARIANTS (same model, same price) collapse into ONE canonical row — routing
  modifiers like `-xhigh` / `-pro20x` / `-thinking` / `-antigravity` /
  `-openai-compact` are stripped before matching (e.g. `gpt-5.5` + its 6
  variants → one `openai:gpt-5.5` row at $5/$30).
* DISTINCT MODELS (different version/tier, different price) keep separate rows
  even when their regex shapes are similar (e.g. `kimi-k2.5` ≠ `kimi-k2.6` ≠
  `kimi-k2.7-code`; `gemini-3.1-pro` ≠ `gemini-3.5-flash`).
"""

import importlib.util
import os

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "detect_model_pricing.py")


def _load_detector():
    spec = importlib.util.spec_from_file_location("bsl_detect_model_pricing_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(providers):
    """Build a minimal config dict shaped like config.yaml."""
    return {
        "providers": {
            pid: {"name": pid.upper(), "models": [{"id": m} for m in models]}
            for pid, models in providers.items()
        }
    }


# ── Variant collapse (same model, same price → ONE row) ───────────────────────

def test_gpt55_variants_collapse_to_one_canonical():
    d = _load_detector()
    cfg = _cfg({
        "vsllm-gpt": ["gpt-5.5", "gpt-5.5-pro20x", "gpt-5.5-pro20x-openai-compact",
                       "gpt-5.5-openai-compact"],
        "vietapi-o": ["gpt-5.5-xhigh", "gpt-5.5", "gpt-5.5-high"],
    })
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    gpt55 = cm.get("openai:gpt-5.5")
    assert gpt55 is not None, "expected openai:gpt-5.5 canonical family"
    # All six variant ids collapse into one family with ONE price.
    assert set(gpt55["patterns"]) == {
        "gpt-5.5", "gpt-5.5-pro20x", "gpt-5.5-pro20x-openai-compact",
        "gpt-5.5-openai-compact", "gpt-5.5-xhigh", "gpt-5.5-high",
    }
    assert gpt55["variant_count"] == 6
    assert gpt55["input_per_1m"] == 5.0
    assert gpt55["output_per_1m"] == 30.0
    # No separate per-variant rows leaked.
    leaked = [k for k in cm if k.startswith("openai:gpt-5.5") and k != "openai:gpt-5.5"]
    assert leaked == []


def test_opus_variants_collapse_to_latest():
    """Opus 4.6/4.7/4.8 (identically priced by Anthropic) collapse to 4.8."""
    d = _load_detector()
    cfg = _cfg({
        "vietapi-a": ["opus-4.8", "opus-4.7", "opus-4.6", "opus-4.8-thinking"],
        "vsllm-a": ["claude-opus-4-6", "claude-opus-4-8", "claude-opus-4.6-thinking",
                     "claude-opus-4.7", "claude-opus-4.8"],
        "vietapi-o": ["claude-opus-4.6", "opus-4.7-thinking",
                      "claude-opus-4-6-antigravity-ultra"],
    })
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    opus = cm["anthropic:claude-opus-4.8"]
    assert opus["variant_count"] == 12
    assert opus["input_per_1m"] == 5.0
    assert opus["output_per_1m"] == 25.0
    assert "claude-opus-4-6" in opus["patterns"]
    assert "opus-4.8-thinking" in opus["patterns"]
    assert "claude-opus-4-6-antigravity-ultra" in opus["patterns"]
    # No separate 4.6/4.7 family rows leaked.
    assert all("opus" not in k or k == "anthropic:claude-opus-4.8" for k in cm)


def test_antigravity_ultra_suffix_strip():
    d = _load_detector()
    cfg = _cfg({"a": ["claude-opus-4-6-antigravity-ultra"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert "anthropic:claude-opus-4.8" in cm
    assert cm["anthropic:claude-opus-4.8"]["patterns"] == ["claude-opus-4-6-antigravity-ultra"]


def test_chained_suffix_strip():
    """A variant carrying MULTIPLE chained modifiers still maps to its family."""
    d = _load_detector()
    cfg = _cfg({"a": ["gpt-5.5-pro20x-openai-compact-antigravity"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert set(cm.keys()) == {"openai:gpt-5.5"}
    assert cm["openai:gpt-5.5"]["input_per_1m"] == 5.0


def test_provider_prefix_stripped():
    d = _load_detector()
    cfg = _cfg({"ckey": ["tanynguyen97/glm-5.2"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    # "tanynguyen97/glm-5.2" collapses to the GLM-5.2 family, not a new one.
    assert "zhipu:glm-5.2" in cm
    assert "tanynguyen97/glm-5.2" in cm["zhipu:glm-5.2"]["patterns"]


def test_anthropic_suffix_stripped():
    """`-anthropic` is a routing modifier (GLM via Anthropic-compatible API)."""
    d = _load_detector()
    cfg = _cfg({"a": ["glm-5.2-anthropic"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert set(cm.keys()) == {"zhipu:glm-5.2"}


# ── Distinct models (different version/tier → SEPARATE rows) ──────────────────

def test_kimi_three_distinct_families():
    """kimi-k2.5 / k2.6 / k2.7-code are DISTINCT models with DIFFERENT prices."""
    d = _load_detector()
    cfg = _cfg({"m": ["kimi-k2.5", "kimi-k2.6", "kimi-k2.7-code"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert {"moonshot:kimi-k2.5", "moonshot:kimi-k2.6", "moonshot:kimi-k2.7-code"} <= set(cm)
    # Manually revised canonical rates (USD/1M tokens).
    assert cm["moonshot:kimi-k2.5"]["input_per_1m"] == 0.50
    assert cm["moonshot:kimi-k2.5"]["output_per_1m"] == 2.50
    assert cm["moonshot:kimi-k2.5"]["cache_hit_per_1m"] == 0.12
    assert cm["moonshot:kimi-k2.6"]["input_per_1m"] == 0.80
    assert cm["moonshot:kimi-k2.6"]["output_per_1m"] == 4.00
    assert cm["moonshot:kimi-k2.6"]["cache_hit_per_1m"] == 0.16
    assert cm["moonshot:kimi-k2.7-code"]["input_per_1m"] == 0.95
    assert cm["moonshot:kimi-k2.7-code"]["output_per_1m"] == 4.00
    assert cm["moonshot:kimi-k2.7-code"]["cache_hit_per_1m"] == 0.19
    # k2.7-code must NOT collapse onto k2.7 (the `-code` is a tier, not a suffix).
    assert all("kimi-k2.7-code" in f["patterns"] for f in [cm["moonshot:kimi-k2.7-code"]])


def test_gemini_distinct_families():
    """gemini-3.1-pro, gemini-3.5-flash, gemini-3-flash are distinct families."""
    d = _load_detector()
    cfg = _cfg({"g": ["gemini-3.1-pro", "gemini-3.5-flash", "gemini-3-flash"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert {"google:gemini-3.1-pro", "google:gemini-3.5-flash", "google:gemini-3-flash"} <= set(cm)
    assert cm["google:gemini-3.1-pro"]["input_per_1m"] == 2.00
    assert cm["google:gemini-3.5-flash"]["input_per_1m"] == 1.50
    # gemini-3-flash: manually revised canonical rates (no longer null/unverified).
    assert cm["google:gemini-3-flash"]["input_per_1m"] == 0.50
    assert cm["google:gemini-3-flash"]["output_per_1m"] == 3.00
    assert cm["google:gemini-3-flash"]["cache_hit_per_1m"] == 0.05
    assert cm["google:gemini-3-flash"]["cache_write_per_1m"] == 0.625
    assert cm["google:gemini-3-flash"]["source_status"] == "manual"


def test_fable_and_opus_are_separate():
    """claude-fable-5 ($10/$50) and claude-opus-4.8 ($5/$25) are distinct."""
    d = _load_detector()
    cfg = _cfg({"a": ["claude-fable-5", "claude-opus-4-8"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert "anthropic:claude-fable-5" in cm
    assert "anthropic:claude-opus-4.8" in cm
    assert cm["anthropic:claude-fable-5"]["input_per_1m"] == 10.0
    assert cm["anthropic:claude-opus-4.8"]["input_per_1m"] == 5.0


def test_fake_maps_to_fable():
    """claude-fake-5 is a typo alias for fable-5 → same family."""
    d = _load_detector()
    cfg = _cfg({"a": ["claude-fake-5", "claude-fable-5"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert set(cm.keys()) == {"anthropic:claude-fable-5"}
    assert set(cm["anthropic:claude-fable-5"]["patterns"]) == {"claude-fake-5", "claude-fable-5"}


def test_gpt54_and_mini_distinct():
    """gpt-5.4 and gpt-5.4-mini are distinct (different price)."""
    d = _load_detector()
    cfg = _cfg({"o": ["gpt-5.4", "gpt-5.4-mini"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert {"openai:gpt-5.4", "openai:gpt-5.4-mini"} <= set(cm)
    assert cm["openai:gpt-5.4"]["input_per_1m"] == 2.50
    assert cm["openai:gpt-5.4-mini"]["input_per_1m"] == 0.75


# ── Free suffix: modifier on some families, distinct marker on others ─────────

def test_free_modifier_collapses_on_minimax():
    """minimax-m3-free is a free-tier variant OF minimax-m3 (same family)."""
    d = _load_detector()
    cfg = _cfg({"m": ["MiniMax-M3", "minimax-m3", "minimax-m3-free"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert set(cm.keys()) == {"minimax:minimax-m3"}
    assert set(cm["minimax:minimax-m3"]["patterns"]) == {"MiniMax-M3", "minimax-m3", "minimax-m3-free"}


def test_free_distinct_on_mimo():
    """mimo-v2.5-free is its OWN family, NOT collapsed onto mimo-v2.5-pro."""
    d = _load_detector()
    cfg = _cfg({"x": ["mimo-v2.5-pro", "mimo-v2.5-free"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert {"xiaomi:mimo-v2.5-pro", "xiaomi:mimo-v2.5-free"} <= set(cm)
    assert cm["xiaomi:mimo-v2.5-pro"]["input_per_1m"] == 0.435
    assert cm["xiaomi:mimo-v2.5-pro"]["cache_hit_per_1m"] == 0.004
    assert cm["xiaomi:mimo-v2.5-free"]["input_per_1m"] == 0.14
    assert cm["xiaomi:mimo-v2.5-free"]["cache_hit_per_1m"] == 0.003


def test_nemotron_ultra_not_stripped():
    """`-ultra` is part of the Nemotron model name (not a routing suffix)."""
    d = _load_detector()
    cfg = _cfg({"n": ["nemotron-3-ultra-free"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    # -free is the modifier (collapsed); -ultra stays as the tier identity.
    assert set(cm.keys()) == {"nvidia:nemotron-3-ultra"}
    assert cm["nvidia:nemotron-3-ultra"]["patterns"] == ["nemotron-3-ultra-free"]


# ── Status / unverified / unknown handling ────────────────────────────────────

def test_gpt56_families_are_distinct_and_strip_routing_suffixes():
    d = _load_detector()
    cfg = _cfg({
        "openai": [
            "gpt-5.6-sol", "gpt-5.6-sol-pro20x", "gpt-5.6-sol-thinking",
            "gpt-5.6-terra", "gpt-5.6-terra-openai-compact",
            "gpt-5.6-luna", "gpt-5.6-luna-pro20x-thinking",
        ],
    })
    cm = d.build_detected(cfg)["canonical_models"]

    assert set(cm) == {
        "openai:gpt-5.6-sol", "openai:gpt-5.6-terra", "openai:gpt-5.6-luna",
    }
    assert set(cm["openai:gpt-5.6-sol"]["patterns"]) == {
        "gpt-5.6-sol", "gpt-5.6-sol-pro20x", "gpt-5.6-sol-thinking",
    }
    assert cm["openai:gpt-5.6-sol"]["input_per_1m"] == 5.0
    assert cm["openai:gpt-5.6-sol"]["output_per_1m"] == 30.0
    assert cm["openai:gpt-5.6-terra"]["input_per_1m"] == 2.5
    assert cm["openai:gpt-5.6-terra"]["output_per_1m"] == 15.0
    assert cm["openai:gpt-5.6-luna"]["input_per_1m"] == 1.0
    assert cm["openai:gpt-5.6-luna"]["output_per_1m"] == 6.0
    assert all(family["source_status"] == "manual" for family in cm.values())


def test_sonnet5_promo_rates_and_expiry_metadata():
    d = _load_detector()
    cm = d.build_detected(_cfg({"anthropic": ["claude-sonnet-5", "claude-sonnet-5-thinking"]}))["canonical_models"]
    sonnet = cm["anthropic:claude-sonnet-5"]

    assert sonnet["input_per_1m"] == 2.0
    assert sonnet["output_per_1m"] == 10.0
    assert sonnet["cache_hit_per_1m"] == 0.2
    assert sonnet["cache_write_per_1m"] == 2.5
    assert sonnet["source_status"] == "manual"
    assert sonnet["pricing_promotion"] == {
        "active_until": "2026-08-31",
        "post_promotion_rates_per_1m": {
            "input": 3.0,
            "output": 15.0,
            "cache_hit": 0.3,
            "cache_write": 3.75,
        },
    }


def test_unknown_model_is_skipped():
    d = _load_detector()
    cfg = _cfg({"weird": ["zzz-not-a-real-model", "gpt-5.5"]})
    out = d.build_detected(cfg)
    cm = out["canonical_models"]
    assert "openai:gpt-5.5" in cm
    assert all("zzz-not-a-real-model" not in f["patterns"] for f in cm.values())


def test_kat_coder_pro_v2_marketplace_family_collapses_all_configured_providers():
    d = _load_detector()
    expected = (
        "kwaipilot:kat-coder-pro-v2", "Kwaipilot", "kat-coder-pro-v2",
        "KAT-Coder-Pro-V2", "manual",
    )
    assert d.classify("kat-coder-pro-v2") == expected
    assert d.classify("kwaipilot/kat-coder-pro-v2") == expected

    cm = d.build_detected(_cfg({
        "iamhc": ["kat-coder-pro-v2", "kwaipilot/kat-coder-pro-v2"],
        "hcnsec": ["kat-coder-pro-v2"],
        "hcnsec-vip": ["kat-coder-pro-v2"],
    }))["canonical_models"]
    kat = cm["kwaipilot:kat-coder-pro-v2"]

    assert kat["canonical_model"] == "kat-coder-pro-v2"
    assert kat["display_name"] == "KAT-Coder-Pro-V2"
    assert kat["input_per_1m"] == 0.30
    assert kat["output_per_1m"] == 1.20
    assert kat["cache_hit_per_1m"] is None
    assert kat["cache_write_per_1m"] is None
    assert kat["source_status"] == "manual"
    assert kat["source_url"] == "https://openrouter.ai/kwaipilot/kat-coder-pro-v2"
    assert set(kat["providers"]) == {"iamhc", "hcnsec", "hcnsec-vip"}
    assert set(kat["patterns"]) == {"kat-coder-pro-v2", "kwaipilot/kat-coder-pro-v2"}
    assert kat["variant_count"] == 2
    assert len(kat["variants"]) == 4


# ── Determinism ───────────────────────────────────────────────────────────────

def test_repeatable_idempotent():
    """Running twice over the same config must yield identical canonical maps."""
    d = _load_detector()
    cfg = _cfg({"vsllm-gpt": ["gpt-5.5", "gpt-5.5-pro20x"],
                "m": ["kimi-k2.5", "kimi-k2.6", "kimi-k2.7-code"]})
    out1 = d.build_detected(cfg)
    out2 = d.build_detected(cfg)
    assert out1 == out2
