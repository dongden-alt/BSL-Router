"""Model scope registry — which sub-models are standardized and where their
trusted anchors live.

Only sub-models with a FROZEN standard on disk can be fully verified
(real / downgrade / substitution). Recognized families without a standard
degrade to family-only confirmation. Unknown inputs return UNVERIFIABLE,
never a false 'fake'.

Standard sources:
  gold      — aibox/* direct vendor API (GLM, Kimi, DeepSeek, Qwen)
  reference — OAuth-real wrappers (Claude via mintrouter, GPT via codex,
              Gemini via antigravity). Real weights + seller persona.
"""
from typing import Optional


# ── Trusted anchors per family ────────────────────────────────────────────
# gold = unwrapped vendor API. reference = real-weights wrapper.
TRUSTED_ANCHORS = {
    "glm":      {"gold": ["aibox/zhipu"],      "reference": ["chat2api/zai-web1", "chat2api/zai-web2"]},
    "kimi":     {"gold": ["aibox/kimi"],        "reference": ["chat2api/workbuddy-web"]},
    "deepseek": {"gold": ["aibox/ds"],          "reference": ["chat2api/deepseek-web"]},
    "qwen":     {"gold": ["aibox/qwen"],        "reference": ["chat2api/qwen-web"]},
    "claude":   {"gold": [],                    "reference": ["chat2api/claude-web", "chat2api/arena-web", "mintrouter"]},
    "gpt":      {"gold": [],                    "reference": ["codex"]},
    "gemini":   {"gold": [],                    "reference": ["antigravity"]},
    # hunyuan / mimo: no trusted in-router anchor yet → deferred (see NOTE)
}


# ── Standardized scope: family → list of sub-model patterns ───────────────
# Patterns are lowercase substrings matched against the suspect model id AND
# the reference hint. More-specific patterns should appear so the narrowest
# variant can be isolated during standard harvest.
SCOPE = {
    "claude":   [
        "fable-5-1", "fable-5.1", "fable-5", "fable",
        "opus-5-5", "opus-5.5", "opus-5",
        "opus-4-8", "opus-4.8", "opus-4-7", "opus-4.7",
        "opus-4-6", "opus-4.6", "opus-4-5", "opus-4.5", "opus-4", "opus 4",
        "sonnet-5-5", "sonnet-5.5", "sonnet-5",
        "sonnet-4-6", "sonnet-4.6", "sonnet-4-5", "sonnet-4.5", "sonnet-4", "sonnet 4",
        "haiku-4-5", "haiku-4.5", "haiku-4", "haiku 4", "haiku-5",
    ],
    "gemini":   [
        "gemini-4-argon", "4-argon", "gemini-4", "gemini 4",
        "gemini-3.8-flash", "gemini-3-8-flash", "3.8-flash",
        "gemini-3.7-flash", "gemini-3-7-flash", "3.7-flash",
        "gemini-3.6-flash", "gemini-3-6-flash", "3.6-flash",
        "gemini-3.5-flash", "3.5-flash",
        "gemini-3.1-pro", "gemini-3-1-pro", "3.1-pro",
        "gemini-pro-agent",
        "gemini-3", "gemini 3", "flash-3", "pro-3", "pro 3",
        "gemini-2.5", "gemini 2.5", "2.5-pro", "2.5-flash", "flash-2.5", "pro-2.5",
    ],
    "gpt":      [
        "gpt-6-astra", "gpt-6.1-sol", "gpt-6-1-sol", "gpt-6-sol", "gpt-6-luna",
        "gpt-5.6-sol", "gpt-5-6-sol", "gpt-5.6-terra", "gpt-5-6-terra",
        "gpt-5.6-luna", "gpt-5-6-luna", "gpt-5.5", "gpt-5-5",
        "gpt-5", "gpt5", "gpt-6", "gpt6", "gpt-6.1", "gpt6.1",
        "-sol", "gpt-sol", "sol-", "terra", "luna", "astra", "o4",
    ],
    "glm":      [
        "glm-5.3-flash", "glm-5-3-flash", "glm-5.3", "glm-5-3",
        "glm-5.2", "glm-5-2", "glm-5.1", "glm-5", "glm5",
        "glm-4.7", "glm-4.6", "glm4.6",
    ],
    "kimi":     [
        "kimi-k3", "kimi k3", "kimi-k2.7-code", "kimi-k2-7-code",
        "k2.7-code", "k2-7-code", "kimi-k2.6", "kimi-k2-6",
        "k2.6", "k2.7", "kimi-k2.5", "k2.5",
        "kimi-k2", "kimi k2", "k3", "k3-", "k3.",
    ],
    "deepseek": [
        "deepseek-v4.1-flash", "deepseek-v4-1-flash", "v4.1-flash", "v4-1-flash",
        "deepseek-v4-pro", "v4-pro", "deepseek-v4-flash", "v4-flash",
        "deepseek-v4", "v4.1", "v4.", "v4-",
        "deepseek-r1", "-r1", "r1-", "/r1",
    ],
    "qwen":     [
        "qwen3.8-max-0902", "qwen3-8-max-0902", "3.8-max-0902",
        "qwen3.8-max", "qwen3-8-max", "3.8-max",
        "qwen3.8-plus", "qwen3-8-plus", "3.8-plus",
        "qwen3.8-flash", "qwen3-8-flash", "3.8-flash",
        "qwen3.7-max", "qwen3-7-max", "3.7-max",
        "qwen3.7-plus", "qwen3-7-plus", "3.7-plus",
        "qwen3.7-flash", "qwen3-7-flash", "3.7-flash",
        "qwen3.6-max", "3.6-max", "qwen3.6-plus", "3.6-plus",
        "qwen3.6-flash", "qwen3-6-flash", "3.6-flash",
        "qwen3", "qwen-3", "3.5-397", "qwen3-max",
        "qwen3.5", "qwen3.6", "qwen3.7", "qwen3.8",
    ],
    "mimo":     ["mimo-v2.5", "mimo v2.5", "v2.5-pro", "mimo-v2.6", "mimo v2.6",
                 "v2.6-pro", "v2.6-flash"],
    # Deferred (no trusted in-router anchor): "hunyuan"
}

# Families explicitly supported but possibly without per-sub-model standards yet
SUPPORTED_FAMILIES = set(SCOPE.keys())


# ── Researched per-variant traits (2026-09/10 sources) ─────────────────────
# These notes tell the standard harvester WHICH variants must be treated as
# distinct TraitMarks because their weights/tuning differ. A seller swapping a
# cheaper variant for a dearer one is exactly what we detect.
VARIANT_NOTES = {
    "claude": {
        "haiku-4-5":  "fast/low-cost tier; shallow multi-constraint and state-trace ceiling",
        "sonnet-4-6": "mid-tier 4.6 balanced coding/reasoning",
        "sonnet-5":   "mid-tier 5.0 generation",
        "sonnet-5-5": "upper-mid 5.5 coding & agentic tier; below Opus/Fable on deep symbolic trace",
        "opus-4-6":   "4.6 flagship reasoning tier",
        "opus-4-7":   "4.7 flagship reasoning upgrade",
        "opus-4-8":   "4.8 flagship reasoning upgrade",
        "opus-5":     "5.0 flagship reasoning tier",
        "fable-5":    "5.0 creative/reasoning flagship",
        "opus-5-5":   "5.5 frontier flagship",
        "fable-5-1":  "5.1 frontier creative + deep multi-constraint flagship",
        "_note": "Split Haiku / Sonnet / Opus / Fable by 3-prong tier_splitter "
                 "(SVG compiler + 4x4 coupled constraint grid + 8-step state trace), "
                 "hard_series, and discriminator.",
    },
    "gpt": {
        # GPT-5.6 generation (tiers Sol/Terra/Luna are DISTINCT models)
        "5.6/sol":   "flagship; ~68% fewer factual errors vs 5.5 Instant",
        "5.6/terra": "mid/balanced, competitive with GPT-5.5",
        "5.6/luna":  "fast/low-cost; free-tier default + Think button",
        "5.6/astra": "separate variant outside the Sol/Terra/Luna ladder",
        # GPT-6 generation (researched 2026-09)
        "6/astra":    "flagship, 2026-09-03; $10/$50 per Mtok; Critical-cyber "
                      "rated; ChatGPT label 'GPT-6 Pro' (Pro/Business/Enterprise)",
        "6/sol":      "mid tier, 2026-09-22; $2/$10; superseded by 6.1 Sol",
        "6/luna":     "budget tier, 2026-09-22; $0.10/$0.50; high-volume; "
                      "below Critical; cheaper than 5.6 Sol",
        "6.1/sol":    "mid tier upgrade, 2026-09-29; $2/$10; ~matches Astra on "
                      "agentic coding/computer-use; 1.05M ctx (922K in), 128K "
                      "out, KNOWLEDGE CUTOFF 2026-04-30 (hard discriminator)",
        "_note": "Each tier is a distinct model, not a quality knob. Split by "
                 "(a) knowledge cutoff (6.1 Sol = 2026-04-30), (b) capability "
                 "ceiling + factual-error rate, (c) context/output caps, "
                 "(d) price-tier behavior. Astra is the Critical-rated flagship; "
                 "selling Astra but serving Sol/Luna is a downgrade.",
    },
    "qwen": {
        "3.6-flash":    "budget MoE flash tier (3.6 series)",
        "3.7-flash":    "budget low-latency SKU (3.7 series)",
        "3.7-plus":     "mid-tier balanced SKU (3.7 series)",
        "3.7-max":      "flagship 3.7 MoE SKU",
        "3.8-flash":    "budget low-latency SKU (3.8 series); frequently substituted for 3.8-max",
        "3.8-plus":     "mid-tier balanced SKU (3.8 series); bridges 3.8-flash and 3.8-max",
        "3.8-max":      "2.4T MoE, 95B active, 1M ctx, GA 2026-08-03",
        "3.8-max-0902": "IN-PLACE upgrade of 3.8-max (2026-09-01), post-trained "
                        "on coding/cowork. TerminalBench 11.3->29.0, "
                        "ProgramBench 10.5->28.0, WorkArena Elo 1348->1468. "
                        "Same weights-family, sharper agentic tuning.",
        "3.8-max-prime":"higher-throughput SKU, same base",
        "_note": "Split 3.7/3.8 flash vs plus vs max via tier_splitter + hard_series "
                 "+ spatial_curve; 3.8-max vs 3.8-max-0902 differ in agentic/coding.",
    },
    "glm": {
        "5.2":       "mid-tier Zhipu GLM-5.2 generation",
        "5.3-flash": "low-cost high-speed GLM-5.3 Flash tier; common downgrade substitute for 5.3",
        "5.3":       "flagship Zhipu GLM-5.3 reasoning & coding model",
        "_note": "Split GLM-5.3-flash vs GLM-5.2 vs GLM-5.3 via tier_splitter "
                 "composite ceiling and hard_series accuracy.",
    },
    "deepseek": {
        "r1":        "reasoning model, distinct from V4 line",
        "v4-flash":  "284B total / 13B active; cheap tier",
        "v4-pro":    "1.6T total / 49B active; flagship; build 0813",
        "v4.1-flash":"552B backbone, 8B read / 16B gen active; 2026-09-10. "
                     "NOTE: V4-Pro traffic now routes to V4.1-Flash at flash "
                     "prices — a 'v4-pro' seller may be serving V4.1-Flash.",
        "_note": "Active-parameter count + reasoning modes + cutoff + tier_splitter "
                 "split Flash vs V4.1-Flash vs Pro. Watch for Pro->V4.1-Flash silent reroute.",
    },
    "kimi": {
        "k2.6":      "budget/earlier Kimi K2.6 chat & coding tier",
        "k2.7-code": "mid-tier Kimi K2.7 coding-specialized SKU",
        "k3":        "flagship Kimi K3 frontier reasoning & multi-step synthesis model",
        "_note": "Split k2.6 vs k2.7-code vs k3 via tier_splitter + hard_series + discriminator.",
    },
    "gemini": {
        "3.1-pro":   "mid-tier Gemini 3.1 Pro reasoning SKU",
        "3.6-flash": "budget Gemini 3.6 Flash high-speed SKU",
        "3.7-flash": "budget Gemini 3.7 Flash high-speed SKU",
        "3.8-flash": "budget Gemini 3.8 Flash high-speed SKU",
        "4-argon":   "flagship Gemini 4 Argon frontier reasoning & spatial-synthesis model",
        "_note": "Split 3.6/3.7/3.8-flash vs 3.1-pro vs 4-argon via tier_splitter "
                 "(SVG compiler + constraint grid + state trace) and spatial_curve.",
    },
    "mimo": {
        "v2.5":      "310B, full-modal, 1M ctx, 2026-04-22",
        "v2.5-pro":  "1.02T total / 42B active; flagship reasoning",
        "v2.6-flash":"310B / 15B active, full-modality, 2026-09-21",
        "v2.6-pro":  "1.02T / 42B active, omni-modal, 2026-09-21; beat Claude "
                     "Opus 5 on automation benchmarks",
        "_note": "V2.6 series is current. Differentiate flash vs pro by "
                 "active-param fingerprint + reasoning depth.",
    },
}


# ── Expected capability tier ceiling per sub-model in [0.0, 1.0] ───────────
# Used by similarity_tier_splitter when a candidate standard predates the
# tier_splitter lane, and by the calibration audit across all 7 families.
MODEL_TIER_EXPECTATIONS = {
    # Budget / Flash / Luna / Haiku (0.30 - 0.42)
    "haiku-4-5":          0.32,
    "gpt-5.6-luna":       0.35,
    "gpt-6-luna":         0.40,
    "qwen3.6-flash":      0.30,
    "qwen3.7-flash":      0.35,
    "qwen3.8-flash":      0.40,
    "glm-5.3-flash":      0.38,
    "deepseek-v4-flash":  0.35,
    "deepseek-v4.1-flash":0.42,
    "kimi-k2.6":          0.42,
    "gemini-3.6-flash":   0.34,
    "gemini-3.7-flash":   0.38,
    "gemini-3.8-flash":   0.42,
    # Mid / Balanced / Plus / Sonnet / Terra / Sol / 5.2 / K2.7-Code / 3.1-Pro (0.58 - 0.74)
    "sonnet-4-6":         0.58,
    "sonnet-5":           0.65,
    "sonnet-5-5":         0.72,
    "gpt-5.5":            0.58,
    "gpt-5.6-terra":      0.62,
    "gpt-5.6-sol":        0.68,
    "gpt-6-sol":          0.68,
    "gpt-6.1-sol":        0.74,
    "qwen3.6-plus":       0.56,
    "qwen3.7-plus":       0.60,
    "qwen3.8-plus":       0.68,
    "glm-5.2":            0.64,
    "kimi-k2.7-code":     0.68,
    "gemini-3.1-pro":     0.68,
    "gemini-pro-agent":   0.72,
    # Flagship / Max / Pro / Opus / Fable / Astra / Argon / K3 (0.82 - 0.96)
    "opus-4-6":           0.82,
    "opus-4-7":           0.85,
    "opus-4-8":           0.88,
    "opus-5":             0.90,
    "fable-5":            0.91,
    "opus-5-5":           0.95,
    "fable-5-1":          0.96,
    "gpt-6-astra":        0.95,
    "qwen3.6-max":        0.80,
    "qwen3.7-max":        0.84,
    "qwen3.8-max":        0.92,
    "qwen3.8-max-0902":   0.96,
    "glm-5.3":            0.88,
    "deepseek-v4-pro":    0.90,
    "kimi-k3":            0.91,
    "gemini-4-argon":     0.95,
}


def _canon_model_key(text: str) -> str:
    import re
    s = normalize(text)
    return re.sub(r"(?<=\d)\.(?=\d)", "-", s)


def get_model_tier_rank(model_id: str) -> Optional[float]:
    """Return expected capability ceiling in [0.0, 1.0] for a sub-model id, or None."""
    c = _canon_model_key(model_id)
    if not c:
        return None
    # Sort longest pattern first so e.g. qwen3.8-max-0902 matches before qwen3.8-max,
    # glm-5.3-flash before glm-5.3, and opus-5-5 before opus-5.
    for pat, rank in sorted(
        MODEL_TIER_EXPECTATIONS.items(),
        key=lambda kv: len(_canon_model_key(kv[0])),
        reverse=True,
    ):
        cp = _canon_model_key(pat)
        if cp in c:
            return float(rank)
    return None


def normalize(text: str) -> str:
    return (text or "").lower().replace("_", "-").strip()


def detect_family(model_name: str) -> Optional[str]:
    """Best-effort family detection from a model name / hint substring.

    Returns the family key or None if nothing recognized. This is a
    *prior* only — the probe evidence can override it.
    """
    n = normalize(model_name)
    if not n:
        return None
    # Explicit family names take precedence over shared version substrings.
    for family in SCOPE:
        if family in n:
            return family
    for family, patterns in SCOPE.items():
        if any(p in n for p in patterns):
            return family
    return None


def in_scope(model_name: str, hint: str = "") -> dict:
    """Decide how much the framework can say about this suspect.

    Returns a dict:
      verdict_scope: "full"     — recognized + (hint or name) maps to a sub-model
                     "family"   — family recognized, no specific sub-model standard
                     "unknown"  — nothing recognized → UNVERIFIABLE
      family:        detected family key or None
      focus:         the narrowed sub-model hint (normalized) or None
      reason:        human-readable explanation
    """
    name_n = normalize(model_name)
    hint_n = normalize(hint)

    fam_from_hint = detect_family(hint_n) if hint_n else None
    fam_from_name = detect_family(name_n)

    family = fam_from_hint or fam_from_name
    if family is None:
        return {
            "verdict_scope": "unknown",
            "family": None,
            "focus": None,
            "reason": f"'{model_name}' not in supported mid/top-tier scope; "
                      f"returning UNVERIFIABLE (not 'fake').",
        }

    focus = hint_n if hint_n else None
    scope = "full" if (hint_n or fam_from_name) else "family"
    if fam_from_hint and fam_from_name and fam_from_hint != fam_from_name:
        # hint and name disagree → flag but proceed, bias to hint
        return {
            "verdict_scope": "full",
            "family": fam_from_hint,
            "focus": focus,
            "reason": f"hint '{hint}' implies {fam_from_hint} but name "
                      f"'{model_name}' implies {fam_from_name}; "
                      f"testing against hint first (hint is a hypothesis, "
                      f"not evidence).",
        }

    return {
        "verdict_scope": scope,
        "family": family,
        "focus": focus,
        "reason": (f"narrowed to {family}"
                   + (f" / focus '{focus}'" if focus else "")
                   + ("." if scope == "full"
                      else " (family-only; no specific sub-model standard).")),
    }
